# University Verification Toolkit

Tools to verify Indian universities and institutions against the two official
regulator lists, capture screenshot proof, and serve everything from a small API.

- **UGC**: the University Grants Commission list of recognised universities
- **AICTE**: the AICTE approved-institutions PDF (Approval Process 2025-26)

## Project layout

| Folder | Purpose |
| --- | --- |
| `ugc_verification/` | `main.py` drives headless Chrome (Selenium) to find a university on the UGC site and save a highlighted screenshot with a proof banner (URL and timestamp). `verify_ugc.py` calls the UGC JSON endpoint directly, with no browser. |
| `aicte_verification/` | `main.py` and `verify_aicte.py` download the AICTE PDF (cached as `aicte_list.pdf`), search it, highlight the match and save a screenshot as proof. |
| `university_db/` | Loads both lists into one MySQL table, then captures screenshots and serves them over a Flask API. |

## Requirements

- Python 3.9+
- Google Chrome plus a matching chromedriver (Selenium 4 can fetch one automatically). Needed only for UGC screenshots.
- MySQL 5.7+ or 8.x. Needed only for `university_db/`.

```bash
python -m venv env
source env/bin/activate        # Windows: env\Scripts\activate
pip install -r requirements.txt
```

## Usage

### UGC check

```bash
# Browser screenshot with proof banner
python ugc_verification/main.py "A.P.J. Abdul Kalam Technological University"
python ugc_verification/main.py "Adani University" --category private -o proofs

# Browser-less lookup against the UGC endpoint
python ugc_verification/verify_ugc.py "CMJ University" --save result.json
```

Useful flags: `--category {all,central,state,deemed,private}`, `--no-filter`,
`--full-page`, `--show-browser`, `--debug`.

### AICTE check

```bash
python aicte_verification/main.py "Amal Jyothi College Of Engineering" -o proofs --full-page
```

The PDF is downloaded on first run and cached. Use `--refresh` to re-download it.

### Institution database and API

1. Configure MySQL. Defaults are a local `root` login with no password.
   Override with `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD` and `MYSQL_DATABASE`.
2. Load both lists. This creates the `university_registry` database from `schema.sql`:
   ```bash
   cd university_db
   python load_institutions.py
   ```
   A UGC university and an AICTE institution with the same normalised name and state
   become a single row. UGC supplies the name and address, and AICTE adds its id, region, district and city.
3. Capture a proof screenshot by name. The database decides whether it is on UGC, AICTE or both:
   ```bash
   python pipeline.py --name "Anjaneya University"
   python pipeline.py --name "Amity University" --state Rajasthan
   ```
   Screenshots are saved to `screenshots/ugc/` and `screenshots/aicte/`.
4. Start the API (`PORT` defaults to 5000):
   ```bash
   python api.py
   ```

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/api/health` | Health check |
| GET | `/api/institutions?q=Anjaneya` | Search institutions |
| GET | `/api/screenshots?name=...` | Primary proof PNG |
| GET | `/api/screenshots/meta?name=...` | Which lists matched, and the files |
| POST | `/api/screenshots?name=...` | Capture screenshots for every matching list |

Add `&state=...` when several institutions share a name.

## Notes

- Screenshots, the downloaded AICTE PDF and the virtualenv are git-ignored. They are regenerated on demand.
- The tools scrape public government sites, so selectors and URLs may break if those sites change.
- The API binds to `127.0.0.1` only and has no authentication. Put it behind a proper gateway before exposing it.
