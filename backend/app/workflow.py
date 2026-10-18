from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, text
from .db import Session, Project, Link, Run, Change, Requirement, Source, log, now
from .intelligence import settings, extract_sources, apply_analysis
from .retrieval import retrieve
from .lifecycle import create_draft, interruption, resolve_interruptions
from . import activity

pool = ThreadPoolExecutor(max_workers=2)


def auto_decide_safe(db, project):
    """Delegation accepts only clear, sourced candidates; never baselines a PRD."""
    if project.state.get("delegation_mode") != "AUTO_DECIDE_SAFE":
        return []
    from . import project_lifecycle
    import re

    accepted = []
    for row in db.scalars(select(Requirement).where(Requirement.project_id == project.id)):
        if row.decision.get("action") in {"ACCEPT", "MODIFY", "REJECT"}:
            continue
        if row.status != "CONFIRMED_CANDIDATE" or not row.evidence:
            continue
        if re.search(r"待確認|待決|尚未|未定|可能|視情況|或許|TBD|[?？]", row.statement, re.I):
            continue
        sources = [db.get(Source, fact.get("source_id")) for fact in row.evidence]
        if any(not source or source.status != "ACTIVE" or source.kind not in {
            "REQUIREMENT_TEXT", "INTERNAL_POLICY", "TECH_SPEC", "BUSINESS_KNOWLEDGE", "USER_UPLOAD",
        } for source in sources):
            continue
        if any(fact.get("kind") in {"OPEN_ITEM", "QUESTION", "EXCLUDED"} for fact in row.evidence):
            continue
        row.decision = {"action": "ACCEPT", "statement": row.statement,
                        "rationale": "使用者授權 Agent 代理；本項來源一致、沒有標示衝突或待確認。正式 PRD 仍需審閱。",
                        "decided_at": now(), "decided_by": "AGENT_DELEGATED"}
        project_lifecycle.memory_for_decision(db, row)
        accepted.append(row.id)
    if accepted:
        log(db, project.id, "AGENT_DECISION", f"Agent 代理接受 {len(accepted)} 項明確需求；未定項目保留人工審查", {"requirement_ids": accepted})
    return accepted


def plan(project):
    if project.state.get("waiting_meeting"):
        return "WAITING_MEETING"
    if project.state.get("analysis_stale", True):
        return "REVISION_ANALYSIS" if project.current_revision_id else "PIPELINE"
    if project.state.get("draft_id"):
        return "REVISION_REVIEW" if project.current_revision_id else "PRD_REVIEW"
    if project.runtime == "BASELINED":
        return "BASELINED"
    return "POST_HUMAN"


def launch(db, project, force_analysis=False):
    if project.status != "ACTIVE":
        raise ValueError("專案已刪除")
    active = db.scalar(
        select(Run).where(
            Run.project_id == project.id,
            Run.status == "RUNNING",
            Run.epoch == project.epoch,
        )
    )
    if active:
        return active
    if force_analysis:
        project.state = {
            **project.state,
            "analysis_stale": True,
            "draft_id": None,
            "dirty_sources": list(
                db.scalars(select(Link.source_id).where(Link.project_id == project.id))
            ),
            "step": 5,
            "progress": 0,
        }
    project.epoch += 1
    project.runtime = "RUNNING"
    launch_config = settings(db)
    from .model_runtime import config_snapshot_id
    run = Run(
        project_id=project.id,
        epoch=project.epoch,
        reason="REVISION" if project.current_revision_id else "INITIAL",
        checkpoint={
            "plan": plan(project),
            "evidence_snapshot": list(project.state.get("dirty_sources", [])),
            "model_config": launch_config,
            "config_snapshot_id": config_snapshot_id(launch_config),
        },
    )
    db.add(run)
    db.flush()
    activity.create(
        db,
        "AI 需求分析" if force_analysis else "Agent 工作流程",
        launch_config,
        project.id,
        run.id,
    )
    log(db, project.id, "RUN", "從已保存的檢查點繼續", run.checkpoint)
    db.commit()
    pool.submit(execute, project.id, run.id, project.epoch)
    return run


def execute(project_id, run_id, epoch):
    try:
        with activity.tracking(run_id):
            _execute(project_id, run_id, epoch)
            with Session() as db:
                run, project = db.get(Run, run_id), db.get(Project, project_id)
                detail = run.error or {
                    "WAITING_HUMAN": "分析完成。請進行人工需求決策或變更審查，再繼續下一步。",
                    "WAITING_MEETING": "等待會議結論；請先分析並確認會議紀錄，再繼續。",
                    "BASELINED": "目前版本已定版，等待新需求或證據。",
                }.get(project.runtime, "處理完成，請檢查草稿或下一步待辦。")
                status = (
                    "FAILED"
                    if run.status == "FAILED"
                    else "CANCELLED"
                    if project.epoch != epoch
                    else "WAITING"
                    if run.status == "WAITING"
                    else "SUCCEEDED"
                )
            activity.finish(run_id, status, detail)
    except activity.Cancelled:
        with Session() as db:
            db.get(Run, run_id).status = "CANCELLED"
            db.commit()


def _execute(project_id, run_id, epoch):
    try:
        with Session() as db:
            project = db.get(Project, project_id)
            if project.epoch != epoch:
                db.get(Run, run_id).status = "CANCELLED"
                db.commit()
                return
            if project.state.get("waiting_meeting"):
                project.runtime = "WAITING_MEETING"
                db.get(Run, run_id).status = "WAITING"
                interruption(
                    db,
                    project,
                    "WAITING_MEETING",
                    "等待會議結論",
                    "狀態已保存。請加入逐字稿、分析並確認會議紀錄，再繼續。",
                )
                db.commit()
                return
            ids = list(
                db.scalars(select(Link.source_id).where(Link.project_id == project_id))
            )
            if not ids:
                project.runtime = "WAITING_HUMAN"
                interruption(
                    db,
                    project,
                    "REQUIREMENT_INPUT_REQUIRED",
                    "請提供需求起點",
                    "輸入需求說明或上傳需求文件，才能開始比對證據。",
                )
                db.get(Run, run_id).status = "WAITING"
                db.commit()
                return
            config = db.get(Run, run_id).checkpoint.get("model_config") or settings(db)
            config = {**config, "_project_id": project_id}
            activity.stage("檢查來源與執行計畫", "正在整理本次需要分析的證據")
            if (
                not project.current_revision_id
                and not project.state.get("retrieval_done")
                and project.mode == "AGENT"
            ):
                # Retrieval precedes intelligence and attaches actual library evidence, never sample facts.
                activity.stage("檢索共用知識庫")
                hits = retrieve(
                    db, project.description or project.name, project.id, config
                )
                added = []
                remote_references = []
                for hit in hits:
                    if hit["evidence_role"] not in {"KEY", "SUPPORTING"}:
                        continue
                    if hit.get("source_location") == "REMOTE":
                        remote_references.append({k: hit.get(k) for k in (
                            "title", "provider_id", "remote_evidence_id", "remote_uri",
                            "case_relevance_score", "evidence_role", "cache_policy")})
                        continue
                    if hit["source_id"] not in ids and hit["source_id"] not in added:
                        db.add(Link(project_id=project_id, source_id=hit["source_id"]))
                        added.append(hit["source_id"])
                ids += added
                project.state = {
                    **project.state,
                    "retrieval_done": True,
                    "retrieval_results": hits,
                    "remote_knowledge_references": remote_references,
                    "dirty_sources": sorted(
                        set(project.state.get("dirty_sources", []) + added)
                    ),
                    "step": 5,
                    "progress": max(project.state.get("progress", 0), 35),
                }
                log(
                    db,
                    project_id,
                    "KNOWLEDGE",
                    f"本機知識檢索完成，加入 {len(added)} 份相關來源",
                    {"source_ids": added},
                )
                log(db, project_id, "ANALYSIS_STARTED", "依來源抽取需求，開始比較證據")
                db.commit()
            dirty = project.state.get("dirty_sources", [])
            if (
                project.mode == "AGENT"
                and config.get("research_enabled")
                and config.get("research_topic")
                and config.get("organization", {}).get("industry")
            ):
                from . import peer_research
                from .db import ResearchRun

                if not db.scalar(
                    select(ResearchRun).where(ResearchRun.project_id == project.id)
                ):
                    peer_research.start(db, project, config["research_topic"])
            stale = project.state.get("analysis_stale", True)
            rejected = set()
            if stale:
                activity.stage(
                    "確認本案來源相關性",
                    "檢查已加入來源的分數與角色；無關的共用文件不作為需求依據",
                )
                assessed = retrieve(
                    db,
                    project.description or project.name,
                    project.id,
                    {**config, "top_k": max(1, len(ids))},
                    "hybrid_reranker",
                    set(ids),
                )
                project.state = {
                    **project.state,
                    "source_qualification": [
                        {
                            k: h[k]
                            for k in [
                                "source_id",
                                "title",
                                "evidence_role",
                                "case_relevance_score",
                                "case_relevance_reason",
                            ]
                        }
                        for h in assessed
                    ],
                }
                from .db import Source

                rejected = {
                    h["source_id"]
                    for h in assessed
                    if h["evidence_role"] == "REJECTED"
                    and db.get(Source, h["source_id"]).meta.get("library_scope")
                    == "GLOBAL"
                }
                ids = [sid for sid in ids if sid not in rejected]
                dirty = [sid for sid in dirty if sid not in rejected]
            log(
                db,
                project_id,
                "ANALYSIS_STARTED",
                "開始來源分析" if stale else "沿用已完成分析，檢查人工決策",
            )
            db.commit()
            extracted = (
                extract_sources(db, project, dirty or ids, config) if stale else {}
            )
            extracted.update({sid: [] for sid in rejected})
        activity.stage(
            "整理分析結果與下一步", "模型輸出已取得，正在核對證據與人工審查條件"
        )
        with Session() as db:
            # Serialize the commit boundary so paused/superseded executions cannot publish stale results.
            db.execute(text("BEGIN IMMEDIATE"))
            project, run = db.get(Project, project_id), db.get(Run, run_id)
            if project.epoch != epoch:
                run.status = "CANCELLED"
                db.commit()
                return
            affected = (
                apply_analysis(db, project, extracted)
                if stale
                else project.state.get("affected_requirements", [])
            )
            from .model_runtime import enabled
            if enabled(config, "evidence_package"):
                from .model_assist import evidence_package
                project.state = {**project.state, "evidence_package": evidence_package(db, project_id)}
            auto_decide_safe(db, project)
            project.state = {
                **project.state,
                "step": 6,
                "progress": max(project.state.get("progress", 0), 60),
            }
            changes = list(
                db.scalars(
                    select(Change).where(
                        Change.project_id == project_id, Change.status == "OPEN"
                    )
                )
            )
            for change in changes:
                relevant = [
                    r.id
                    for r in db.scalars(
                        select(Requirement).where(Requirement.project_id == project_id)
                    )
                    if any(f["source_id"] == change.source_ref for f in r.evidence)
                ]
                change.impact = {
                    "affected_requirement_ids": relevant or affected,
                    "affected_prd_sections": [
                        "requirements",
                        "traceability",
                        "meetings",
                    ],
                    "affected_systems": [],
                    "impact_level": "HUMAN_REVIEW_REQUIRED",
                }
            if changes:
                interruption(
                    db,
                    project,
                    "CHANGE_REVIEW",
                    "新證據可能影響已定版 PRD",
                    "請檢查變更影響，決定建立修訂或保留追溯但不改版。舊版內容不會改寫。",
                )
                project.runtime = "WAITING_HUMAN"
            elif any(
                r.decision.get("action") not in {"ACCEPT", "MODIFY", "REJECT"}
                for r in db.scalars(
                    select(Requirement).where(Requirement.project_id == project_id)
                )
            ):
                interruption(
                    db,
                    project,
                    "REQUIREMENT_REVIEW",
                    "需求比對完成，需要你的決策",
                    "請檢查來源證據並接受、修改或拒絕候選需求。衝突與未定項目不會自動定案。",
                )
                project.runtime = "WAITING_HUMAN"
            elif project.current_revision_id and not db.scalar(
                select(Change).where(
                    Change.project_id == project_id, Change.status == "REVIEWED"
                )
            ):
                project.runtime = "BASELINED"
            else:
                create_draft(db, project)
            resolve_interruptions(db, project, ["REQUIREMENT_INPUT_REQUIRED"])
            run.status = "SUCCEEDED"
            run.checkpoint = {
                **run.checkpoint,
                "affected_requirements": affected,
                "plan": plan(project),
            }
            db.commit()
        if enabled(config, "agent_split"):
            activity.stage("Agent 證據審閱", "比對需求與來源，產生供人工審閱的建議")
            from .model_assist import judge_requirements
            try:
                with Session() as review_db:
                    review_project = review_db.get(Project, project_id)
                    judgement = judge_requirements(review_db, review_project, config)
                with Session() as save_db:
                    review_project = save_db.get(Project, project_id)
                    if review_project.epoch == epoch:
                        review_project.state = {**review_project.state, "agent_judgement": judgement}
                        save_db.commit()
            except Exception as exc:
                with Session() as save_db:
                    review_project = save_db.get(Project, project_id)
                    if review_project and review_project.epoch == epoch:
                        review_project.state = {**review_project.state, "agent_judgement": {
                            "status": "ERROR", "error": str(exc)[:300], "items": []}}
                        save_db.commit()
        if (config.get("model_router") or {}).get("decision", {}).get("mode") == "advisory":
            activity.stage("Decision 建議分析", "供人工參考，不會自動批准需求")
            from .decision_provider import advise_project
            try:
                with Session() as review_db:
                    advice = advise_project(review_db, review_db.get(Project, project_id), config)
            except Exception as exc:
                advice = {"status": "ERROR", "error": str(exc)[:300], "items": []}
            with Session() as save_db:
                review_project = save_db.get(Project, project_id)
                if review_project and review_project.epoch == epoch:
                    review_project.state = {**review_project.state, "decision_advice": advice}
                    save_db.commit()
    except activity.Cancelled:
        raise
    except Exception as exc:
        with Session() as db:
            run = db.get(Run, run_id)
            project = db.get(Project, project_id)
            if run and project and project.epoch != epoch:
                run.status = "CANCELLED"
            elif run:
                run.status, run.error = (
                    "FAILED",
                    (
                        str(exc)
                        or f"{type(exc).__name__}：模型回應逾時或服務中斷，請檢查模型後重試"
                    )[:2000],
                )
            if project and project.epoch == epoch:
                project.runtime = "ERROR"
                log(db, project.id, "ERROR", str(exc)[:2000])
            db.commit()
