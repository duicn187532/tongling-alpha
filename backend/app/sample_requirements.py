"""Invent a fictional but realistic requirement so a new user can try the flow.

Model calls run at temperature 0 with a fixed seed, so variety comes from a
randomly chosen business area and angle sent with each request. Without a
model, one of a few hand-written fictional samples is returned.
"""

import random

from pydantic import BaseModel, Field

AREAS = ["信用卡", "存款帳戶", "個人信貸", "房屋貸款", "企業網銀", "數位帳戶開戶", "基金理財", "外匯", "行動支付", "分行預約"]
ANGLES = ["客戶通知", "線上申請流程", "身分驗證", "交易限額", "客訴處理", "對帳報表", "權限管理", "資料保存與稽核", "優惠活動", "異常交易提醒"]


class Sample(BaseModel):
    name: str = Field(min_length=4, max_length=40)
    brief: str = Field(min_length=40, max_length=600)


FALLBACKS = [
    {"name": "信用卡分期申請進度通知",
     "brief": "目前客戶在 App 申請帳單分期後，只能打客服查詢審核進度。本次需求：審核結果出來時以推播與簡訊通知客戶，"
              "推播內容不可顯示完整卡號（僅末四碼）；客戶可在 App 查看申請紀錄。通知需保留寄送紀錄供稽核，預計下一季上線。"},
    {"name": "企業網銀大額轉帳雙人覆核",
     "brief": "企業網銀單筆轉帳目前只需經辦一人送出。本次需求：單筆超過企業設定門檻的轉帳，需由另一位授權主管覆核後才能執行；"
              "覆核逾時自動取消並通知經辦。門檻由企業管理者在後台設定，所有送出與覆核動作需留存軌跡。"},
    {"name": "數位帳戶開戶身分驗證改版",
     "brief": "數位帳戶線上開戶目前以上傳證件照片人工審核，平均需兩個工作天。本次需求：導入他行帳戶驗證作為第二身分驗證方式，"
              "驗證成功即可完成開戶；驗證失敗三次則轉人工審核。需符合個資法與開戶相關規範，並保留驗證紀錄。"},
    {"name": "定存到期提醒與續存設定",
     "brief": "客戶定存到期後若未設定續存，會自動轉入活存，常有客戶反映錯過利率。本次需求：到期前 7 天以 App 推播與 email 提醒，"
              "客戶可在提醒中直接設定到期續存或解約；未設定者維持現行轉入活存。提醒內容不得顯示完整帳號。"},
]


def generate(config, rng=random):
    area, angle = rng.choice(AREAS), rng.choice(ANGLES)
    fallback = rng.choice(FALLBACKS)
    if config.get("provider") == "mock":
        return {**fallback, "area": None, "method": "RULES"}
    from .model_runtime import structured
    try:
        result = structured(
            config, "PRD_ASSIST",
            "你是台灣金融業的業務需求單位窗口，正在用自然的口吻向 PM 提出一個新需求。請虛構一個合理、具體、可實作的需求，"
            "主題落在指定的業務領域與面向。name 是 8–20 字的專案名稱。brief 寫成 3–5 句通順的繁體中文段落："
            "先說目前客戶或行員遇到的具體狀況，再說這次要改的具體行為（誰、在什麼情況、做什麼），最後提一到兩個限制"
            "（例如個資、資安、稽核、上線時間）。不要出現「現況」「痛點」「本次要改什麼」「關鍵限制」等標題字樣，"
            "不要條列；不得出現真實銀行、公司或人名；數值要合理。",
            {"business_area": area, "angle": angle},
            Sample, lambda: {"name": fallback["name"], "brief": fallback["brief"]},
            timeout_seconds=120, max_tokens=700)
        return {**result.model_dump(), "area": f"{area}・{angle}", "method": "MODEL"}
    except Exception as exc:
        return {**fallback, "area": None, "method": "RULES", "warning": f"模型暫時無法產生，改用內建範例：{type(exc).__name__}"}
