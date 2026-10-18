# 彤靈談心 TongLing Alpha

彤靈談心是可在個人 Windows 主機執行的金融需求整理與 PRD 草稿工具。它把文件檢索、來源追溯、Agent 輔助、人工決策、技術設計與 IT 交付檢查放在同一個工作流程。這是 **Alpha**：AI 產出是待審建議，正式金融規則、系統設計與簽核仍需 PM／SA／QA 核對。

主機同時是管理端與客戶端。其他使用者可透過受限的客戶端入口配對，各自只看自己的專案；主機可提供經管理者允許的共用知識庫。資料庫、上傳文件、設定、模型與金鑰都保留在執行主機，不屬於原始碼套件。

## 快速開始（Windows）

需要 Python 3.12、Node.js 22.12 以上（含 npm）。Ollama 與 ngrok 都是選用；首次安裝 Python／Node 依賴需連網。將此儲存庫放在可寫入的資料夾，在 PowerShell 或檔案總管啟動：

```powershell
.\start.cmd
```

第一次啟動會建立 `.venv`、安裝鎖定的依賴、建置前端並建立本機 `data/`。之後每次會檢查依賴版本、重新建置前端及執行資料庫遷移。管理工作台只綁定 `127.0.0.1:8000`；請勿把 8000 轉送到公網。若不想自動開瀏覽器，可執行 `powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser`。

關閉主機與由本套件啟動的客戶端／ngrok 通道：

```powershell
.\stop.cmd
```

只關閉遠端客戶端通道：`powershell -ExecutionPolicy Bypass -File .\Stop-ClientAccess.ps1`。啟動視窗保持前景時也可用 `Ctrl+C` 關閉主機。詳細步驟與故障排除見 [部署指南](docs/DEPLOYMENT.md)。

## 遠端客戶端

先在主機啟動本機管理端，並自行安裝 ngrok、在**自己的終端機**設定 authtoken。然後執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\Start-ClientAccess.ps1
```

腳本會在 `127.0.0.1:8001` 啟動受限入口，建立 HTTPS 通道；配對碼只在主機顯示。遠端使用者可以選手動或 Agent 模式，但不能管理主機設定。只有客戶端入口 8001 可連到通道，絕不可把 8000 指向 ngrok。配對與專案仍儲存在主機本地，關閉或重啟主機不會把資料上傳到 GitHub。

## 使用範圍

- 匯入文件或掃描專用資料夾，使用混合、向量及可選圖譜檢索，保留證據來源與相關性判斷。
- 從一句需求開始，由 Agent 提示下一步，或改走手動分析、人工確認與 PRD 修訂。
- 在技術設計中先檢索本案 SA 來源；沒有來源時可上傳或輸入，再由 AI 產生附原文段落的待審候選。
- 產生可編輯 PRD、技術模型與圖表；IT 交付包須通過版本及審閱檢查。
- 主機可設定 Ollama 或 OpenAI 相容模型；預設離線規則模式只用於流程驗證，不能代替語意分析。

已知限制：目前以單台 Windows 主機與 SQLite 為主要部署形態；沒有提供託管雲服務、企業 SSO 或正式簽核身份驗證。模型回答與圖表必須由人審查。公共網路存取依賴 ngrok 帳戶與其流量限制。架構與信任邊界見 [架構說明](docs/ARCHITECTURE.md)。

## 隱私與開發

執行資料一律放在 `data/` 或使用者指定的 `workspace_files/`；兩者、備份、日誌、虛擬環境、前端依賴與任何 `.env` 都不納入 Git。請不要在 issue、PR 或測試樣本中提交真實客戶資料、內部文件、金鑰或 ngrok 配對資訊。範例文件位於 `sample_data/demo_pack/`，明確標示為虛構資料。

後端測試：`cd backend; ..\.venv\Scripts\python.exe -m pytest -q`。前端檢查：`cd frontend; npm run build`。安全問題請依 [SECURITY.md](SECURITY.md) 私下回報；參與方式見 [CONTRIBUTING.md](CONTRIBUTING.md)。

原始碼採 [Apache License 2.0](LICENSE)。第三方依賴由各自授權條款管理。
