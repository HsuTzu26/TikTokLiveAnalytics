# TikTok LIVE Analytics

[繁體中文 README](README.md) | English

TikTok LIVE Analytics is a locally run tool for collecting TikTok LIVE events, reviewing stream activity, and comparing sessions. It is an independent community project and is not affiliated with or endorsed by TikTok or TikTool.

## What it does

- **Watch** — monitors configured streamers and starts collection when a LIVE session is detected.
- **Collect** — records available chat, viewer, member/join, like, gift, room, social, and system events as NDJSON.
- **Analyze** — provides live dashboards, trends, cross-session comparisons, and daily session consolidation.
- **Reports** — generates streamer-oriented summaries and downloadable interactive HTML reports.
- **Optional bilingual TTS** — can read selected chat messages while following an existing collector session; it does not open another LIVE connection.
- **Optional chat sender** — can automate the TikTok web interface using Playwright in a dedicated local Chrome profile. See [CHAT_SENDER.md](CHAT_SENDER.md).

The collector records events exposed by the upstream service and SDK. It cannot recover events that were never delivered, and it does not claim complete TikTok-side audience or revenue data.

## Data sources, APIs, and responsibilities

| Data or feature | Source and implementation | Important limitation |
| --- | --- | --- |
| LIVE events | The TikTool-maintained [tiktok-live-events Python SDK](https://github.com/tiktool/tiktok-live-events), using TikTool's managed WebSocket service at wss://api.tik.tools | This is not TikTok's official LIVE developer API. Available event types and completeness depend on the upstream SDK/service and TikTok. |
| Room and ranking snapshots | TikTool REST API at [api.tik.tools](https://api.tik.tools); the src/snapshots.py module uses /webcast/room_info, /webcast/rankings, and /webcast/gift_info | Requires TIKTOOL_API_KEY; availability and fields depend on the service and account permissions. Snapshots are not a substitute for a complete event history. |
| Optional TikTok cookie | TIKTOK_COOKIE_HEADER is sent to the TikTool API as x-cookie-header when configured | A cookie is a sensitive login credential. Use only your own authorized account, keep it local, and never commit or share it. |
| LIVE status probe | The application parses publicly accessible TikTok LIVE webpage HTML | This is a best-effort web-page probe, not an official API or a guarantee of LIVE status. |
| Chat sending | Playwright automates the TikTok website UI in a user-local Chrome profile | No TikTool message-sending API or official TikTok sending API is used or claimed. The user must log in, complete any verification, and have authorization to post in the selected room. |
| Text-to-speech | The optional TTS feature uses [edge-tts](https://github.com/rany2/edge-tts), which accesses Microsoft's online Edge TTS service | Selected chat text is sent to an external online TTS service; this is not offline speech synthesis. |

### API keys and privacy

- Set TIKTOOL_API_KEY only in a local environment or an untracked secrets file.
- Set TIKTOK_COOKIE_HEADER only if you understand and need the optional cookie-backed API request. Treat it like a password.
- Never commit API keys, cookies, or browser session data.
- The data directory is excluded from Git. The dedicated browser profile under data/chat_sender/chrome_profile/ may contain active login state and must not be uploaded or shared.
- If TTS is enabled, the text selected for speech is transmitted to the online TTS provider.

## Requirements and setup

The project is developed with Python 3.12 and a local .venv. On Windows, run these commands from the project directory. The upstream SDK is a separate Git project:

~~~powershell
git clone https://github.com/tiktool/tiktok-live-events.git
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
~~~

Then open http://127.0.0.1:8501 in a browser.

The application and dashboard are intended to run locally. Do not expose the Streamlit server to an untrusted network without adding appropriate security controls.

## Tests

Run the test suite from the project root:

~~~powershell
.\.venv\Scripts\python.exe -m pytest
~~~

## Reading the collected data

- All displayed and stored application timestamps are normalized to Taiwan time (Asia/Taipei) where applicable.
- Viewer values are observations/snapshots, not necessarily unique viewers.
- Join/member events are event counts; they do not prove a count of unique people.
- Likes and other cumulative values can begin mid-stream if collection starts after the LIVE has begun.
- Gift diamonds are an observed gift-value metric, not TikTok's official settlement, creator payout, or income.
- Connection uptime and event counts describe the collector's observed window. A healthy connection does not prove that the entire stream was captured without gaps.
- Data availability depends on what TikTok exposes and what the upstream SDK/API delivers at collection time.

## Project references

- [Project plan](TikTok_LIVE_Analytics_Project_Plan.md)
- [Bilingual real-time TTS technical design](docs/TikTok_LIVE_RealTime_Bilingual_TTS_Technical_Design.docx)
- [TTS runtime notes](docs/TTS_RUNTIME.md)
- [Chat sender setup and safety notes](CHAT_SENDER.md)

## Upstream documentation and libraries

- [TikTool tiktok-live-events SDK](https://github.com/tiktool/tiktok-live-events)
- [TikTool API documentation](https://tik.tools/docs)
- [TikTool WebSocket guide](https://tik.tools/guides/tiktok-live-websocket)
- [Playwright Python persistent browser context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [edge-tts](https://github.com/rany2/edge-tts)
- [Streamlit](https://docs.streamlit.io/)
- [Altair](https://altair-viz.github.io/)

## Acknowledgments

Thanks to the TikTool maintainers for the SDK and service documentation, and to the maintainers of edge-tts, Streamlit, Playwright, Pandas, Altair, Matplotlib, and Pygame for their open-source work. Their projects remain subject to their own licenses and terms.

## License and attribution

Original project code in this repository is released under the [MIT License](LICENSE), unless a file states otherwise. The MIT license applies to this project's original code; it does not grant rights to TikTok or TikTool services, data, trademarks, third-party dependencies, user-provided documents, collected LIVE data, or media. Those remain subject to their respective owners' rights and applicable terms.
