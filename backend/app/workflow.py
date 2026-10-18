import re
from sqlalchemy import select, text
from .db import Session, Project, Link, Run, Change, Requirement, Source, log, now
from .intelligence import settings, extract_sources, apply_analysis
from .retrieval import retrieve, _retrieve_local
from .knowledge_federation import agent_search, import_remote_evidence
from .lifecycle import create_draft, interruption, resolve_interruptions
from . import activity

def requirement_text(db, project):
    """What the user asked for: the brief plus requirement texts added later."""
    parts = [project.description or ""]
    for source in db.scalars(select(Source).join(Link, Link.source_id == Source.id).where(
            Link.project_id == project.id,
            Source.kind.in_(["REQUIREMENT_TEXT", "REQUIREMENT_DOCUMENT"]))):
        if source.content and source.content.strip() not in parts[0]:
            parts.append(source.content.strip())
    return "\n".join(p for p in parts if p)[:2000] or project.name


def auto_decide_safe(db, project):
    """Delegation accepts only clear, sourced candidates; never baselines a PRD."""
    if project.state.get("delegation_mode") != "AUTO_DECIDE_SAFE":
        return []
    from . import project_lifecycle
    import re

    accepted = []
    proposals = {item["requirement_id"]: item for item in
                       (project.state.get("change_analysis") or {}).get("proposals", [])}
    for row in db.scalars(select(Requirement).where(Requirement.project_id == project.id)):
        if row.decision.get("action") in {"ACCEPT", "MODIFY", "REJECT"}:
            continue
        if row.status != "CONFIRMED_CANDIDATE" or not row.evidence:
            continue
        if proposals and proposals.get(row.id, {}).get("status") not in {"ADD", "ALREADY_SUPPORTED"}:
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
        row.decision = {"action": "ACCEPT", "statement": proposals.get(row.id, {}).get("proposed_requirement", {}).get("statement") or row.statement,
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
    from . import job_queue
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
    job = job_queue.submit(db, "AGENT", project=project,
                           payload={"run_id": run.id, "epoch": project.epoch},
                           idempotency_key=f"agent:{run.id}")
    run.checkpoint = {**run.checkpoint, "job_id": job.id}
    log(db, project.id, "RUN", "從已保存的檢查點繼續", run.checkpoint)
    db.commit()
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
            if not project.state.get("retrieval_done") and project.mode == "AGENT":
                # The Agent searches the shared library itself before analysis:
                # local shared documents and any enabled company file server.
                activity.stage("檢索共用知識庫", "以需求主題搜尋內建知識庫與公司檔案服務器")
                hits, queries = agent_search(
                    db, project, requirement_text(db, project),
                    {**config, "_skip_graph_reasoning": True}, _retrieve_local,
                )
                added, imported, references, failures = [], [], [], []
                for hit in hits:
                    if hit["evidence_role"] not in {"KEY", "SUPPORTING"}:
                        continue
                    if hit.get("source_location") == "REMOTE":
                        if len(imported) >= 5:
                            continue
                        try:
                            source, created = import_remote_evidence(
                                db, project, hit["provider_id"], hit["remote_evidence_id"],
                                config, hit.get("matched_query", ""))
                        except Exception as exc:  # keep a reference the user can add later
                            failures.append(f"{hit['title']}：{str(exc)[:120]}")
                            references.append({k: hit.get(k) for k in (
                                "title", "provider_id", "remote_evidence_id", "remote_uri",
                                "case_relevance_score", "evidence_role", "cache_policy")})
                            continue
                        if source.id not in ids and source.id not in added:
                            added.append(source.id)
                            imported.append(source.id)
                        db.commit()  # do not hold the write lock across the next fetch
                        continue
                    if hit["source_id"] not in ids and hit["source_id"] not in added:
                        db.add(Link(project_id=project_id, source_id=hit["source_id"]))
                        added.append(hit["source_id"])
                ids += added
                project.state = {
                    **project.state,
                    "retrieval_done": True,
                    "retrieval_queries": queries,
                    "retrieval_results": [{k: h.get(k) for k in (
                        "source_id", "title", "evidence_role", "case_relevance_score", "case_relevance_reason",
                        "source_location", "provider_id", "remote_evidence_id", "matched_query")}
                        for h in hits],
                    "remote_knowledge_references": references,
                    "dirty_sources": sorted(
                        set(project.state.get("dirty_sources", []) + added)
                    ),
                    "step": 5,
                    "progress": max(project.state.get("progress", 0), 35),
                }
                local_added = len(added) - len(imported)
                summary = (f"共用知識檢索完成（搜尋詞：{'、'.join(queries) or project.name}）："
                           f"找到 {len(hits)} 筆，加入本機 {local_added} 份、公司檔案服務器 {len(imported)} 份")
                if failures:
                    summary += f"；{len(failures)} 份遠端文件僅保留參考"
                log(db, project_id, "KNOWLEDGE", summary,
                    {"source_ids": added, "imported": imported, "queries": queries, "failures": failures})
                log(db, project_id, "ANALYSIS_STARTED", "依來源抽取需求，開始比較證據")
                db.commit()
                # Activity rows live in another session; write only after committing.
                activity.stage("檢索共用知識庫", summary)
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
                    {**config, "top_k": max(1, len(ids)), "_skip_graph_reasoning": True},
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
            # Sources the user removed retire their assertions in the same apply.
            extracted.update({sid: [] for sid in project.state.get("removed_sources", [])})
            if stale and not project.current_revision_id and re.search(
                r"API|電文|資料表|資料庫|介面|系統架構|程式碼|欄位",
                project.description + " ".join(f.get("statement", "") for facts in extracted.values() for f in facts),
                re.I,
            ):
                activity.stage("檢索技術知識", "查找可引用的 SA／系統規格，缺少證據時保留待確認")
                try:
                    technical_hits = retrieve(
                        db, project.description + " 系統 API 電文 資料表 技術規格", project.id,
                        {**config, "top_k": 5, "_skip_graph_reasoning": True}, "hybrid_reranker",
                    )
                    technical_ids = list(dict.fromkeys(
                        hit["source_id"] for hit in technical_hits
                        if hit.get("source_location") != "REMOTE"
                        and hit.get("evidence_role") in {"KEY", "SUPPORTING"}
                        and hit.get("source_id") not in ids
                        and (source := db.get(Source, hit.get("source_id"))) is not None
                        and source.kind == "TECH_SPEC" and source.status == "ACTIVE"
                    ))
                    if technical_ids:
                        technical_facts = extract_sources(db, project, technical_ids, config)
                        for source_id in technical_ids:
                            db.add(Link(project_id=project_id, source_id=source_id))
                        extracted.update(technical_facts)
                        project.state = {**project.state, "technical_retrieval_source_ids": technical_ids}
                        db.commit()
                except Exception as exc:
                    project.state = {**project.state, "technical_retrieval_error": str(exc)[:300]}
                    db.commit()
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
            if stale and project.state.get("removed_sources"):
                project.state = {**project.state, "removed_sources": []}
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
            if project.current_revision_id and project.state.get("change_analysis_unchanged"):
                for change in changes:
                    change.status = "NO_PRD_CHANGE"
                    change.resolution = {"reason": "新增來源未改變需求語意或關鍵風險；保留既有決策與已定版 PRD",
                                         "resolved_at": now(), "decided_by": "SEMANTIC_IMPACT_CHECK"}
                if changes:
                    log(db, project_id, "CHANGE", "新增支持性證據未改變需求，沿用既有決策與基準")
                changes = []
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
            elif project.state.get("change_analysis") and not (project.current_revision_id and not changes) and not (
                project.state.get("delegation_mode") == "AUTO_DECIDE_SAFE"
                and not project.state["change_analysis"].get("critical_questions")
                and project.state["change_analysis"].get("model_status") != "PENDING"
                and all(r.decision.get("action") in {"ACCEPT", "MODIFY", "REJECT"}
                        for r in db.scalars(select(Requirement).where(Requirement.project_id == project_id)))
            ) and (
                (project.state.get("change_review") or {}).get("status") != "APPROVED"
                or (project.state.get("change_review") or {}).get("fingerprint") != project.state["change_analysis"].get("fingerprint")
            ):
                report = project.state["change_analysis"]
                interruption(
                    db, project, "REQUIREMENT_REVIEW", "AI 需求變更提案待整體審閱",
                    f"有 {sum(1 for item in report.get('proposals', []) if item.get('needs_review', True))} 項變更需要你審閱；其中 {len(report.get('critical_questions', []))} 項須明確決策，其餘可一次接受。",
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
        if config.get("provider") != "mock":
            from .change_analysis import enrich_report
            with Session() as read_db:
                read_project = read_db.get(Project, project_id)
                report = (read_project.state or {}).get("change_analysis") or {}
            if report.get("model_status") == "PENDING":
                activity.stage("生成變更提案", "整理正式需求、驗收條件與可比較方案")
                try:
                    suggestions = enrich_report(report, {**config, "_project_id": project_id})
                    outcome = "COMPLETE"
                    error = ""
                except Exception as exc:
                    suggestions, outcome, error = [], "ERROR", str(exc)[:300]
                with Session.begin() as save_db:
                    save_project = save_db.get(Project, project_id)
                    if save_project and save_project.epoch == epoch:
                        current_report = dict((save_project.state or {}).get("change_analysis") or {})
                        if current_report.get("fingerprint") == report.get("fingerprint"):
                            enhanced = {item["requirement_id"]: item for item in suggestions}
                            for proposal in current_report.get("proposals", []):
                                suggestion = enhanced.get(proposal["requirement_id"])
                                if suggestion:
                                    proposal["proposed_requirement"] = {
                                        "statement": suggestion["formal_statement"],
                                        "acceptance_criteria": suggestion["acceptance_criteria"] or
                                                               ["待 PM 確認可驗證的成功與例外情境"]}
                                    proposal["alternatives"] = suggestion["alternatives"]
                                    proposal["risks"] = list(dict.fromkeys(proposal["risks"] + suggestion["risks"]))
                                    proposal["model_recommendation_reason"] = suggestion["recommendation_reason"]
                            current_report["model_status"] = outcome
                            current_report["model_error"] = error
                            save_project.state = {**save_project.state, "change_analysis": current_report}
                            if save_project.state.get("delegation_mode") == "AUTO_DECIDE_SAFE" and not save_project.current_revision_id and not current_report.get("critical_questions"):
                                rows = list(save_db.scalars(select(Requirement).where(Requirement.project_id == project_id)))
                                if rows and all(row.decision.get("action") in {"ACCEPT", "MODIFY", "REJECT"} for row in rows):
                                    for row in rows:
                                        proposal = next((p for p in current_report["proposals"] if p["requirement_id"] == row.id), None)
                                        if proposal and row.decision.get("decided_by") == "AGENT_DELEGATED":
                                            row.decision = {**row.decision, "statement": proposal["proposed_requirement"]["statement"]}
                                    create_draft(save_db, save_project)
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
