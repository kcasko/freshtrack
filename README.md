# FreshTrack: Food Bank and Mobile Pantry Access Routing

Real food help in Kalamazoo County, Michigan: a Strands agent that answers "where can I get food, right now?" in plain language, in the person's own language, plus an explorable map with routing to the nearest sites.

## The FreshTrack agent (Strands Agents SDK)

Pantry hours in Kalamazoo County are things like "4th Saturday, 9am" or "Mondays, but not the 5th Monday." Someone with no car and no data plan can't work that out from a PDF, and one wrong time can mean a long bus ride to a locked door. FreshTrack takes a message like *"I'm in 49007 and need groceries tonight, no car"* and replies with what is open now or later today, how far it is, and who to call.

```
person -> Navigator agent ---------> Hours guard ----> reply
          (Strands, Bedrock)         (deterministic)
           tools:                        | invented time or phone?
            find_food_help               v
            route_to_site          Fact-checker agent
            site_details           (sees only recorded tool results)
                                         | still fails?
                                         v
                                   safe fallback: Loaves & Fishes + 2-1-1
```

- **The tools do the facts, the model does the talking.** `data/schedules.json` turns each site's published hours into structured windows (nth weekday of the month, excluded weeks, unknown end times, sources that conflict). `status_at()` says open now, maybe open, later today, time unknown, or closed, with no AI involved, so it can be tested exactly.
- **Amazon Location Service v2.** `geo-places` Geocode turns a street, landmark, or ZIP into a point. `geo-routes` CalculateRoutes gives real walking and driving time. If routing fails, the agent gives straight-line miles and says so. It never guesses a travel time.
- **Hours guard.** Every clock time and phone number in a reply is checked against the dataset. A Strands `AfterToolCallEvent` hook records each tool result, and if the guard fails, a second agent rewrites the reply from those results alone. Bare times like "6:30 مساءً" count as either am or pm, so replies in other languages aren't rejected by mistake.
- **Made for texting.** Short replies, 6th-grade reading level, the reply comes back in whatever language the person wrote in, no markdown, never asks about income or immigration status.

### Results

`python eval/run_eval.py --agent --baseline`, Claude Haiku 4.5 on Amazon Bedrock, 8 realistic requests in English, Spanish, and Arabic, each with the clock pinned to a specific time in November 2026. The baseline is the same model and system prompt with no tools.

| Metric | FreshTrack | Same model, no tools |
|---|---|---|
| Named a right place for that time and need | **8/8** | 2/8 |
| Replies with an invented time or phone number | **0/8** | 5/8 |
| Reply in the person's language | 8/8 | 8/8 |
| Median reading grade (English replies) | **4.7** | 12.1 |
| Median words | 66 | 80 |
| Median response time | 11.9 s | 6.7 s |

Deterministic checks, with no AWS needed: 44/44 schedule checks against expected answers worked out by hand from the published hours (first, fifth, and nth-weekday rules, conflicts, unknown times), and 5/5 search checks. Full replies are in `eval/results.json`.

Known limits: the guard checks times and phone numbers, not which site a number is attached to. That rule lives in the prompt, and the evaluation shows the model following it, but nothing enforces it. Eight agent cases is a small sample.

### Run the agent

Requires Python 3.10+ and AWS credentials with Bedrock access to Claude Haiku 4.5 in us-east-1, plus `geo-places:Geocode` and `geo-routes:CalculateRoutes`.

```
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m freshtrack_agent.chat --now "2026-11-03 17:00" --show-guard
python eval/run_eval.py           # offline checks
python eval/run_eval.py --agent --baseline
```

`--now` pins the clock for demos. Leave it off to use the real time in Kalamazoo. Set `FRESHTRACK_MODEL` to use a different Bedrock model, and `FRESHTRACK_OFFLINE=1` to skip AWS (ZIP codes only, straight-line distance).

## The map: what it does

- Filter by service type: emergency pantry, soup kitchen, senior help, mobile distribution
- Type an address and find the nearest 3 sites
- Click a pin for address, notes, hours, phone, and source
- Click the map to set a starting point, or use your location, and see sites sorted by distance
- Click Calculate route on any card for a driving or walking route
- Clusters dense areas at high zoom, expanding into pins with hours and intake notes

## Dataset

`data/places.geojson` is the dataset. Starter set, October 30, 2026.

- 14 emergency pantries in Kalamazoo, Portage, and Comstock
- 2 soup kitchens in downtown Kalamazoo: Kalamazoo Gospel Mission and Ministry with Community
- Senior and mobile programs noted on Kalamazoo Loaves & Fishes, and a mobile distribution noted at the Salvation Army

Primary local sources: South Michigan Food Bank Kalamazoo County Resource Guide, Kalamazoo Loaves & Fishes, and local resource directories. Each card links to its source where one was found.

Dataset layers named for the next version, not yet in this file: Feeding America open agency data, USDA Food Access Research Atlas, and Michigan Open Data emergency food sites. Those would add a low-access area layer to compare against pantry locations, but only after their licenses, file formats, and tract boundaries are checked. They are not claimed as current data here.

This is not an exhaustive directory. Hours and distribution times change. Most hours here come from secondary directories and resource lists, not from a direct call to each site, and cards flag conflicts plainly, for example Twelve Baskets, Stone Church, and Westwood.

The earlier grocery and farmers market version is saved as `data/places-food-access-backup.geojson` in case you want to add those layers back later.

## AWS pieces

1. Amazon Location Service, Maps and Routes
   - Map resource for the basemap, shown through MapLibre GL JS
   - Place Index for address search and for geocoding new dataset addresses
   - Route Calculator for driving and walking routes, with distance and time
2. Amazon S3 plus CloudFront
   - S3 hosts `index.html`, `config.js`, `data/places.geojson`, and the Lambda bundle notes
   - CloudFront in front of S3 for the public URL, caching, and HTTPS
3. AWS Lambda, optional but wired
   - `lambda/nearest.py` takes a typed address, geocodes it with the Place Index, finds the nearest 3 sites, and adds driving routes for those 3
   - The frontend uses the Lambda Function URL when `lambdaEndpoint` is set in `config.js`. Without it, the frontend falls back to Place Index search in the browser when a key is set, and to map clicks or geolocation otherwise. It never invents a drive time: without a route calculator, it shows straight-line miles and says so.

## Run it locally

From this folder:

```
python3 -m http.server 8000
```

Then open http://localhost:8000

Do not just double-click `index.html`. Browsers block the GeoJSON fetch from a file URL.

## Connect Amazon Location Service

In the AWS console, in us-east-1 or your chosen region:

1. Create a map named `freshtrack-kalamazoo`.
2. Create a place index named `freshtrack-place-index`.
3. Create a route calculator named `freshtrack-route-calculator`.
4. Create an API key for those three resources. Restrict it to those resources and, for a public site, to your site domain.
5. Copy the key into `config.js` as `apiKey`. Do not commit a real key to a public repo. For a shared demo, rotate the key after judging, or switch to Amazon Cognito.
6. Reload. The banner at the top should say the Amazon Location basemap is active, address search should work, and Calculate route should return road distance and time instead of a straight line.

To geocode a new address for the dataset, use the Amazon Location Place Index in the console or AWS CLI, then add the returned coordinates to `data/places.geojson`. Never guess coordinates from a street name alone.

## Deploy to S3 and CloudFront

1. Create an S3 bucket for static website hosting.
2. Upload `index.html`, `config.js`, and the `data/` folder. Do not upload a config with a real API key to a public repo; use a restricted key for the deployed site only.
3. Create a CloudFront distribution with the S3 bucket as origin, and test the CloudFront URL from a fresh browser.
4. Optional: deploy `lambda/nearest.py` as a Lambda Function URL, with IAM permission only for SearchPlaceIndexForText and CalculateRoute on your resources, and put that URL in `config.js` as `lambdaEndpoint`.
5. Set a billing alarm in AWS Billing before you share the link, and note the resources to delete after the challenge if you do not want to keep the site up.

## Strands Agents social good hackathon checklist (due November 4)

- [ ] Strands SDK workshop completed on Builder Center between October 28 and November 4 (required to qualify)
- [ ] Public GitHub repo with the agent source, no keys committed
- [ ] Builder Center project article: the problem, who it helps, the agent design, the results table, and what you learned
- [ ] Public demo video under 3 minutes, showing the agent answering live, including one non-English request and the guard trace
- [ ] Submission framed under the Health focus area (food insecurity and nutrition)

## Map submission checklist

- [ ] Live CloudFront or S3 link works from a fresh browser, no login needed
- [ ] Dataset description: what is in it, where it came from, what is left out, and which layers are future work
- [ ] AWS write-up: why Amazon Location for maps, Place Index search, and Routes; why S3 plus CloudFront for hosting; what Lambda adds
- [ ] Public GitHub repo, with no API key committed
- [ ] Demo video under 3 minutes: open FreshTrack, filter to soup kitchens, type an address and find the nearest 3, calculate a driving route, then a walking route, click a card with a call-ahead warning
- [ ] Submitted before Monday, November 2 at 1:00 p.m. PT (4:00 p.m. Detroit time)

## Demo video outline, about 2 minutes

1. Open the live map, say what question it answers in one sentence: where is the nearest food help, and how do I get there?
2. Filter to soup kitchens, click Kalamazoo Gospel Mission, point out meal times and the call-ahead note.
3. Type an address, find the nearest 3, click Calculate route, show driving, then switch to walking.
4. Filter to senior help, click Loaves & Fishes, point out that senior commodities are at community centers and residential sites, not just the warehouse.
5. Close with what is next: a low-access layer from USDA or Feeding America data, and a pantry list verified directly with each site.

## What to improve after the deadline

- Add the USDA Food Access Research Atlas low-access layer, so the map shows gaps, not just pins.
- Add bus access notes, since not everyone drives to a pantry.
- Add a data freshness date per place, not just one date for the whole file.
- Verify every pantry directly with the organization, and remove any site that cannot be confirmed.
