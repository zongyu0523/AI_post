# AGENTS.md — 給 AI 看的專案說明

這份文件是寫給要修改這個專案的 AI 助手（Codex / Claude Code / Cursor…）看的。
人類看的安裝與使用說明在 `README.md`。

讀完這份再動手。裡面有一半是**量過的事實**，推翻它們需要新的量測，不是推理。

---

## 0. 這是什麼

排程發文工具。寫好文字 + 選圖 + 設時間 → 時間到自動發到 **Facebook**（瀏覽器，LLM agent 操作）
或 **LINE 群組記事本**（桌面，模板比對）。單機跑，單頁 UI，SQLite。

使用者是非技術人員。**UI 上不可以出現技術術語，不可以有 JSON 編輯器。**

---

## 1. 不可以動的核心不變量

### 1.1 沒有證據，不算成功

全部在 `gate.py` 的 `settle()`，一個函式三個分支：

| 有證據 | 開火階段 | 結果 |
|---|---|---|
| 有 | 任何 | `success` |
| 沒有 | `unknown`（發布鍵按下去了） | `uncertain` —— 人去看，**系統不自動重試** |
| 沒有 | `not_fired`（還沒按） | `failed` —— 可證明沒發出去，重跑安全 |

兩條規則，改動時必須維持：

1. **「那行 click 沒丟例外」不是證據。** 證據只能由 `attempt.prove()` 產生，而且只能來自
   程式讀回畫面，不能來自 agent 的自述。
2. **只有「能證明沒開火」才可以重試。** 頻道在按下發布的**前一行**呼叫 `attempt.firing()`。

`uncertain` 是一等公民的終局狀態，不是一種失敗。重複發文對使用者是真實傷害。

### 1.2 agent 不能宣告自己成功

`BrowserAgent` 的 `done()` 只結束它的回合，**不產生證據**。驗證一律由 `channels/` 裡的程式做。
`tests/test_fb_audience.py::test_the_agent_cannot_be_the_one_who_confirms` 是這條的守門：
它用 `inspect.getsource` 檢查 `_verify_published`（會喚醒 agent 的那個）裡**不准有**
`attempt.prove`，而 `_found_it`（產生證據的那個）裡**不准有** `operator`。

### 1.3 內文是程式打的，不是模型打的

`type_body` 用 `post.body` 原文填入。模型：

- **拿不到內文**（任務裡沒有預覽，連 marker 都不給）
- `type` 打到 `role=textbox` 時，若 `body_typed` 已經是 True → 直接 `REFUSED`

這是因為真的出過事：任務裡曾經給模型看 `post.marker`（剝掉 emoji 的內文），
模型判定編輯器裡的 emoji 是「多餘字元」，用 `type` 覆寫，**把使用者的 emoji 刪掉發出去**。
光靠 prompt 措辭擋不住，所以才有程式層的 `REFUSED`。

---

## 2. 關於 Facebook 的量測事實

**這一節是量出來的，不是推論的。** 任何想「改良」驗證邏輯的修改，先重讀這裡。

### 2.1 DOM 是刻意不可讀的

在真實的 Facebook 上量過：

```
社團頁 div[role="article"] 數量：0
個人檔案的：2 個，但 text / href / 時間戳全是空的
整頁 role 分布：none:227, button:182, link:79, gridcell:26, row:14 …（沒有 article）
時間戳「13 分鐘」拆成多個 span，抓不到這個字串
class 名稱全是 x1n2onr6 x1ja2u2z 這類亂碼
社團動態消息排序方式：最相關（不是時間！新貼文不保證在最上面）
```

**所以：不要再寫解析貼文區塊的程式碼。** 曾經有一整套 `articles()` / `is_fresh()` /
時間戳判斷，全部刪掉了 —— 它們只在自己寫的測試 fixture 上成功過，從來沒在真的 FB 上成功過。

### 2.2 穩的只有兩樣東西，都是字串

```
1. 整頁 innerText                    ← 內文以文字渲染，混淆打不掉
2. href 裡的伺服器 id                 ← set=pcb.900000000000001（同一則貼文的四張圖共用）
```

驗證就是比對這兩樣的「發文前 vs 發文後」，實作在 `channels/facebook_page.py::_found_it`，
只有兩條判準：

```python
# 1. 多了一個發文前沒有的貼文 id，而且 id 比基準線裡所有的都大
# 2. 多了一段發文前沒有的文字
```

配套的三條規則，刪掉任何一條都會產生假成功：

- **基準線空的就什麼都不證明。** 空的意思是「沒讀到這一頁」，不是「這一頁本來沒貼文」。
  對著空集合比，上禮拜的貼文也是新的。
- **id 必須比基準線裡最大的還大。** FB 的 id 隨時間遞增（21:57 那則 `900000000000001`
  < 22:07 那則 `900000000000002`）。這擋掉「基準線只渲染 2 則、驗證時渲染 5 則，
  多出來的舊貼文被當成新的」。
- **基準線要等頁面渲染完再抓。** 沒渲染完就抓，之後什麼都會看起來是新的 —— 比沒有基準線更糟。

### 2.3 巢狀 dialog

FB 的編輯器**裡面**還有一個 `role="dialog"` —— 它自己的標題列（60px 高，只有標題和關閉鍵）。
取「DOM 順序最後一個 dialog」會取到標題列，模型看到的 outline 只有兩行，輸入框完全消失。

規則在 `pagetools.py::_DIALOG_JS`，**一份定義，拼進三個地方**（snapshot / scroll / upload），
而且 `_with_dialog_rule()` 在拼不進去時會直接 `raise`：

```js
// 巢狀的丟掉，剩下的取最後。往外取不會少東西（外層本來就包含內層），往裡取會丟掉整個編輯器
const open = [...document.querySelectorAll('[role="dialog"]')].filter(dialogShowing);
return open.filter(d => !open.some(o => o !== d && o.contains(d)));
```

### 2.4 通知面板

每個 FB 頁面的 DOM 裡都有「通知」和 Messenger 面板，**裝滿別人的貼文 id**，而且每次載入都不一樣。
`post_ids()` 會跳過「不顯示中的 dialog」裡面的連結，否則別人的一則新通知就會被當成你的貼文。

### 2.5 分享對象

- **個人動態**：有「貼文分享對象」控制項，程式會讀它、改成設定值，改不動就 `Refused` 不發
- **粉絲專頁**：沒有這個控制項（粉專貼文一律公開）→ 跳過
- **社團**：有，但 `disabled`（成員都看得到，不能改）→ 跳過

判斷方式是**看畫面**，不是看網址。因為 `facebook.com/zhang.ming.123` 這種個人帳號網址
跟粉專網址長得一模一樣，照網址放行會讓個人貼文沒人擋地公開發出去。

「找不到控制項」和「讀不到畫面」是兩件事：前者才當粉專處理，後者要擋下來
（靠 `has_no_audience()` 檢查快照裡有沒有 textbox）。

### 2.6 emoji 與特殊字元

FB 把 emoji 畫成 `<img>`，所以它們**不在** `innerText` 裡。`store.text_runs()` 把內文切成
「FB 會當成文字渲染」的段落：

```
'gegege3️⃣4️⃣5️⃣' → ['gegege']        鍵帽是三個碼位（數字+FE0F+20E3），ASCII 數字也要一起拿掉
'🎉🎉🎉'          → []                全是 emoji → 沒有文字可比對
'𝗍𝗲𝘁𝘀𝗲𝘁'        → ['𝗍𝗲𝘁𝘀𝗲𝘁']        數學字體是文字，不是 emoji，不可以剝掉
'龍𠮷學'          → ['龍𠮷學']         罕用漢字同理
```

`_PICTOGRAPH` 曾經寫成 `code > 0xFFFF`，把數學字體和罕用漢字當成 emoji 全殺掉，
一則貼文的搜尋字串因此只剩一個字元 `'t'`。現在只剝 emoji 區段。

---

## 3. 架構

```
app.py              FastAPI：UI、排程 ticker、開機清理、per-runtime 佇列
gate.py             證據閘 —— 核心不變量，動它之前讀 §1.1
store.py            SQLite + 寫入邊界驗證 + text_runs/marker
agent.py            BrowserAgent：LLM 工具迴圈、迴圈守衛、內文保護
pagetools.py        observe→act：snapshot / click / fill / upload / scroll / post_ids
browser.py          Playwright 持久 profile + 反自動化偵測旗標
linewin.py          LINE 的 Win32 層（PrintWindow / PostMessage / WM_CHAR / 模板比對）
llm.py              LLM 設定（端點寫死，使用者不需要知道）
vision.py           LINE 驗證用的畫面辨識
channels/
  base.py           頻道契約
  facebook_page.py  FB（個人動態 / 粉專 / 社團），agent 驅動 + 程式驗證
  line_note.py      LINE 群組記事本，recipe 驅動
static/index.html   單頁 UI，原生 JS，零建置
tests/              110 個測試
```

### 刻意的選擇，不要「改良」掉

- **單頁 HTML、沒有建置步驟** —— 這個規模不需要 React。
- **SQLite 不是 JSON 檔** —— 這些列就是「到底發出去了沒」的紀錄。
- **一次性排程，不是 cron** —— 使用者要的是「這篇在這個時間發」。
- **錯過超過 60 分鐘標 `missed`，不補發** —— 程式沒開是常態，隔天亂發比沒發更糟。
- **一次只跑一個發文**（`asyncio.Lock` per runtime）—— 一顆 Chrome profile 獨佔鎖，
  LINE 只有一個視窗。排隊中的工作在 UI 上顯示「排隊中」而不是「執行中」，
  否則看起來像當掉，使用者會去砍掉重練。

---

## 4. agent 迴圈守衛

真的燒掉過整輪的迴圈，每一個都有對應的守衛（在 `agent.py`）：

| 守衛 | 擋什麼 |
|---|---|
| `clicked` 連點同一個元素 3 次 | 在分享對象選單裡繞了 20 輪 |
| `scrolls > 3` 連續捲動 | 發文後去動態牆找自己的貼文，捲了 12 次 |
| `same_views >= 2 / >= 5` 畫面沒變 | 看了 5 次一模一樣的畫面（捲動夾在中間，連續捲動守衛抓不到） |
| `publish` 立刻 `raise Stop` | 按下發布就是它的最後一件事，之後由程式去確認 |
| `type` 擋覆寫內文 | §1.3 |

**提早死掉比燒完 26 步有用。** 報「畫面沒變過」比報「步數用完」有資訊量。

---

## 5. 改東西的規矩

### 5.1 測試綠不等於對

這個專案最大的教訓：**整套驗證邏輯曾經測試全綠，但對真的 Facebook 完全無效** ——
因為 fixture 是照著自己的假設寫的。

所以：

1. **動到跟外部網站互動的邏輯，要對真實頁面量過。** 寫一個 read-only 的探針腳本，
   開 `browser.open_context(PROFILE_DIR / "facebook")`，載入頁面，把你的假設印出來。
   不要只看測試。
2. **守衛測試要證明它能紅。** 把修法拿掉跑一次，確認對應的測試變紅。不會紅的守衛是裝飾品。
   

### 5.2 跑測試

```bash
python -m pytest -q                      # 110 passed（會開幾個短命的 Chrome）
python -m pytest tests/test_fb_verify.py -q
```

Windows 上輸出中文要 `PYTHONIOENCODING=utf-8`，否則 cp950 會炸。

### 5.3 prompt 慣例

**模型看的字串一律英文，agent 回話用使用者的語言（預設 zh-TW）。**
`attempt.step()` 的文字是給使用者看的，所以是中文；`_goal()` 和工具回傳值是給模型看的，所以是英文。



### 5.4 不要用 Bash heredoc 寫含跳脫字元的程式碼

`\n`、`\b` 在 heredoc 裡會被吃掉，產生壞掉的字串字面值。用檔案編輯工具。

---

## 6. 已知未解決的問題

1. **小編用粉專身分發到社團，驗證會失敗。**
   `verify_url()` 用 `c_user` cookie 組出「這個成員在這個社團的貼文」頁，
   但那是**登入的人**，不是**發文的身分**。粉專身分發的文不會出現在那一頁 → 回報 `uncertain`。
   貼文本身會發出去，只是報告不準。
   根治方法是改用「發佈請求有沒有送出去」當判準（見下）。

2. **發佈請求還沒被當成判準。**
   `_watch_graphql()` 已經在錄（名稱 + HTTP 狀態 + 時間），`_calls_at_publish()` 會在
   驗證失敗時印出按下發布前後 30 秒的呼叫。但**哪一個呼叫代表「貼文建立成功」還沒量到**
   —— 需要一次真實發文的資料。拿到之後，這條可以取代整套「去某頁比對」，
   連同問題 1 一起消失。

3. **全部都是 emoji 又沒有圖片的貼文驗不出來。** 沒有文字可搜、沒有圖片 id。

---
