# GeM Price Comparison

Paste a GeM (Government e-Marketplace) product link. The tool reads the page,
searches Amazon.in, Flipkart and other Indian shops, and tells you whether the
same product is cheaper there. It shows the result as soon as it has one, then
updates it while the slower shops and one retry finish.

The LLM does one job only: reading the product and judging whether a listing is
the same product. All fetching is plain code. Nothing is guessed: an unknown
price stays unknown.

## One-minute start (Windows, VS Code)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env                     # put NVIDIA_API_KEY in it
python tools/doctor.py           # every line should be PASS or WARN
python cli.py "https://mkp.gem.gov.in/..."
```

In VS Code: open the folder, `Ctrl+Shift+P` → "Python: Select Interpreter" →
`.venv`, then use the integrated terminal. The project needs Python 3.10 or
newer (3.12 is what CI uses).

## Keys

Two example files, both copied and kept out of git:

| File to copy | What goes in it | Needed? |
|---|---|---|
| `.env.example` → `.env` | `NVIDIA_API_KEY=...`, optional settings | Yes, for anything that calls the LLM |
| `llm_providers.example.json` → `llm_providers.json` | the providers and their models | Optional; without it, only NVIDIA from `.env` is used |

Get a free NVIDIA key at https://integrate.api.nvidia.com. More than one
provider is better: when one is busy or out of allowance the router uses the
next. See [docs/llm-providers.md](docs/llm-providers.md).

## Commands

| Command | What it does |
|---|---|
| `python cli.py "<gem link>"` | Compare and print the result, then the `Updated:` lines |
| `python cli.py "<gem link>" --refresh` | Ignore the saved result and run again |
| `python cli.py "<gem link>" --no-wait` | Print the first result and exit, no retry watching |
| `python cli.py "<gem link>" --json` | The whole result as JSON |
| `python cli.py "<gem link>" --strict` | Slow, high-recall mode (see below) |
| `python -m uvicorn api.server:app --port 8000` | Web page on http://127.0.0.1:8000 |
| `GEM_DEMO=1 python -m uvicorn api.server:app` | Web page with fake data, no scraping, no LLM |
| `python tools/doctor.py` | Offline health check |
| `python tools/doctor.py --live` | Live: LLM endpoints, search engines, one canary per shop |
| `python tools/doctor.py --clean` | Remove old traces, run records and debug pages |
| `python tools/probe_endpoints.py` | One line per LLM endpoint (finish reason, rate-limit header names) |
| `python tools/save_page.py flipkart "usb c cable"` | Save a real page as a test fixture |
| `python -m eval.run_eval --key eval/answer_key.csv --label first` | Measure against your answer key |
| `python -m pytest -q` | The whole test suite |

Exit codes of the CLI: `0` success, `1` the run failed, `2` the link is not a
GeM product link (nothing was fetched).

## Normal mode and `GEM_STRICT`

Normal mode is the fast one: shops are searched at the same time, a cheap
filter keeps the best listings per shop, and four listings are checked at once.
The result appears as soon as one shop answers; the retry updates it later.

`--strict` (or `GEM_STRICT=1`) turns the filter off, sends every listing to the
LLM and checks one at a time. It is slower and more expensive. Use it when you
think the filter may have hidden a real match.

## Web page

`python -m uvicorn api.server:app --port 8000` is enough. The server binds
`127.0.0.1` by default because there is no login. Paste a link and the page
fills in shop by shop. The recent-runs list survives a restart, and an old run
can be opened again.

Demo mode (`GEM_DEMO=1`) runs the real pipeline with fake pages, so you can work
on the page with no keys and no network.

## Known limits

- Amazon and Flipkart fight bots. A shop can answer with a CAPTCHA; the tool
  says so on that shop's card and tries once more, then stops. It does not use
  a proxy pool.
- Prices come from the page text. A page that hides the price in an image, or
  shows a range, may give no price or the low end of the range. Nothing is
  invented; unknown stays `null`.
- One Chrome window at a time per run. Two runs together work; five do not.
- The match verdict is an LLM's judgement. Cheap/expensive checks and a
  confidence threshold guard it, but a wrong answer is still possible.
- Only the shops that the search engines return are checked. A shop that
  blocks search engines is never seen.
- The numbers are a snapshot, not a guarantee. Delivery, seller and condition
  are not compared.
- Measuring accuracy needs a hand-made answer key of 30-40 products. Without
  that, the accuracy numbers are stories, not measurements.
- Nothing here is legal advice about procurement rules.

## Where things are

- `core/pipeline.py` — one entry point for CLI and API; everything else is a
  helper.
- `docs/architecture.md` — the flow, the shops, the retry, result v2.
- `docs/settings.md` — every setting, its default and its risk.
- `docs/api.md` — routes and events for the web page.
- `docs/troubleshooting.md` — blocked shops, CAPTCHA, LLM limits, missing Chrome.
- `docs/evaluation.md` — how to measure recall and precision.
- `docs/llm-providers.md` — adding a provider.
- `docs/DECISIONS.md` — why the code does what it does.
