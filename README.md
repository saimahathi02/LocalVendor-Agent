# LocalVendor-Agent

Voice-first AI procurement assistant for small vendors. One order in, ranked and budget-aware supplier deals out,
vendor approves, and only then are the suppliers messaged.

Stack (as in the PPT): Python + FastAPI, HTML/CSS/JavaScript, Gemini API, JSON storage,
speech-to-text (browser Web Speech API, Gemini for WhatsApp voice notes), WhatsApp Cloud API (simulator first).

## Run it (Windows PowerShell)

    cd localvendor-agent
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env
    python -m uvicorn main:app --reload

Open http://127.0.0.1:8000. With an empty `.env` everything works offline (rule-based parser, template negotiation
chat, WhatsApp simulator). Use Chrome or Edge for the Speak button.

## Files

    main.py            FastAPI backend: the 5 agents, pricing rules, ranking, approval, WhatsApp, metrics
    static/index.html  page
    static/style.css   responsive styles (3 columns on desktop, stacked on phones)
    static/app.js      voice input, live agent steps, ranked offers, Approve / Reject, simulator, metrics
    data/              created on first run: suppliers.json (prices, stock, quality) and quotes.json (quotes + decisions)

## How it follows the PPT

| PPT | Where |
|---|---|
| Vendor Input Agent: voice note or text | Text box, Speak button, WhatsApp text / voice note (webhook or simulator) |
| Requirement Agent: items, quantities, budget, deadline | `parse_order()` (Gemini, with rule-based fallback) |
| Supplier & Price Agent: price, stock, quality, delivery | `market()` + eligibility checks (3 suppliers in `data/suppliers.json`) |
| Negotiation Agent: bundle offers, counteroffers | `build_plans()`, `price()`, `threads()` (stretch round if over budget) |
| Decision & Approval Agent: ranks deals, asks to confirm | `rank()`, top 3 offers, Approve / Reject |
| One-tap approval link | `/?quote=<id>`, sent to the vendor on WhatsApp |
| Guardrail: never orders without approval | `do_approve()` is the only code that messages suppliers |
| Itemized total, delivery, budget remaining, % below budget | Approval card |
| Metrics: extraction accuracy, budget compliance, on-time rate, approval rate | Metrics panel + `/api/metrics`, `/api/selftest` |

Ranking: feasible deals first (within budget, on time, under your max unit price), then lowest cost adjusted by
2% per quality star below 5, then fewest suppliers.

## Real WhatsApp (Cloud API)

1. developers.facebook.com: create an app, add the WhatsApp product, open WhatsApp > API Setup.
2. Copy the temporary access token to `WHATSAPP_TOKEN` and the Phone number ID to `WHATSAPP_PHONE_ID`.
3. In the "To" list add your own phone and verify it. Put the same number in `DEMO_WHATSAPP_TO` (digits only, e.g. 919876543210).
4. From your phone, send "hi" to the test number once. Free-form text is only delivered inside the 24-hour window after you message it.
5. Restart the server, run an order, approve. The supplier orders arrive on your phone.
6. To receive orders by WhatsApp: run `ngrok http 8000`, set `BASE_URL` to the https address, and in Meta
   WhatsApp > Configuration set Callback URL `https://<address>/webhook`, Verify token = `WHATSAPP_VERIFY_TOKEN`,
   and subscribe to `messages`. Voice notes need `GEMINI_API_KEY`.

## Deploy (simple web hosting, e.g. Render)

Build command `pip install -r requirements.txt`. Start command `uvicorn main:app --host 0.0.0.0 --port $PORT`.
Set the environment variables from `.env.example` in the host's dashboard and set `BASE_URL` to the site address.
Free hosts reset the `data/` folder on restart, which is fine for a demo.
