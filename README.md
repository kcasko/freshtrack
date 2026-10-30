# FreshTrack: Food Bank and Mobile Pantry Access Routing

[![offline checks](https://github.com/kcasko/freshtrack/actions/workflows/offline-checks.yml/badge.svg)](https://github.com/kcasko/freshtrack/actions/workflows/offline-checks.yml)
![Strands Agents SDK](https://img.shields.io/badge/Strands_Agents-SDK-0969da)
![Amazon Bedrock](https://img.shields.io/badge/Amazon-Bedrock-ff9900)
![Amazon Location](https://img.shields.io/badge/Amazon-Location_Service-ff9900)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Real food help in Kalamazoo County, Michigan: a Strands agent that answers "where can I get food, right now?" in plain language, in the person's own language, plus an explorable map with routing to the nearest sites.

> **you:** I'm in 49007 and need groceries tonight. I don't have a car.
>
> **FreshTrack:** Good news! Valley Family Church opens in 30 minutes at 5:30pm tonight for groceries. It's 4 miles away. Since you don't have a car, call them at 269-324-5599 to ask about getting there.
>
> If you need food right now, Kalamazoo Gospel Mission is open now for dinner (4:30-6:30pm) and is very close to you. Call 269-345-2974 to confirm.

*A real reply from the evaluation, with the clock pinned to Tuesday, November 3, 5:00pm.*

## Try it in 5 minutes

You need Python 3.10+ and AWS credentials with Amazon Bedrock access to Claude Haiku 4.5 in us-east-1.

```
git clone https://github.com/kcasko/freshtrack && cd freshtrack
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m freshtrack_agent.chat --now "2026-11-03 17:00" --show-guard
```

No AWS account? `python eval/run_eval.py` runs every offline check (schedule logic, search, hours guard) without credentials.

## The FreshTrack agent (Strands Agents SDK)

Pantry hours in Kalamazoo County are things like "4th Saturday, 9am" or "Mondays, but not the 5th Monday." Someone with no car and no data plan can't work that out from a PDF, and one wrong time can mean a long bus ride to a locked door. FreshTrack takes a message like *"I'm in 49007 and need groceries tonight, no car"* and replies with what is open now or later today, how far it is, and who to call.

![FreshTrack architecture: a navigator agent with tools, a deterministic hours guard, a fact-checker agent, and a safe fallback](docs/architecture.svg)

- **The tools do the facts, the model does the talking.** `data/schedules.json` turns each site's published hours into structured windows (nth weekday of the month, excluded weeks, unknown end times, sources that conflict). `status_at()` says open now, maybe open, later today, time unknown, or closed, with no AI involved, so it can be tested exactly.
- **Amazon Location Service v2.** `geo-places` Geocode turns a street, landmark, or ZIP into a point. `geo-routes` CalculateRoutes gives real walking and driving time. If routing fails, the agent gives straight-line miles and says so. It never guesses a travel time.
- **Hours guard.** Every clock time and phone number in a reply is checked against the dataset, and each phone number must belong to the site it is given for, so the agent can't hand out one pantry's number for another. It also flags promises about what pantries ask, like "they don't ask about immigration status." A Strands `AfterToolCallEvent` hook records each tool result, and if the guard fails, a second agent rewrites the reply from those results alone. Bare times like "6:30 مساءً" count as either am or pm, so replies in other languages aren't rejected by mistake.
- **Made for texting.** Short replies, 6th-grade reading level, the reply comes back in whatever language the person wrote in, no markdown, no "let me check" narration, never asks about income or immigration status.
- **Safety first.** A medical emergency gets 911 first. Thoughts of suicide get 988 (Suicide and Crisis Lifeline) first. A location outside the county gets 2-1-1.

### Results

`python eval/run_eval.py --agent --baseline --runs 3`, Claude Haiku 4.5 on Amazon Bedrock. 30 realistic requests, each run 3 times (90 replies), in English, Spanish, Arabic, French, Swahili, Vietnamese, and Chinese, with the clock pinned to a specific time in November 2026. They include a medical emergency, a suicidal message, an undocumented person asking if they qualify, a Chicago address, an appointment-only pantry, a pantry with no listed time, and sources that disagree. The baseline is the same model and system prompt with no tools.

| Metric (90 replies) | FreshTrack | Same model, no tools |
|---|---|---|
| Passed every check | **90/90** | 4/90 |
| Cases that passed on all 3 runs | **30/30** | 0/30 |
| Named a right place for that time and need | **90/90** | 27/90 |
| Wrong time, phone, or unsupported promise delivered | **0/90** | 54/90 |
| Safety and case rules met (911, 988, 2-1-1, appointment, call first) | **90/90** | 82/90 |
| Reply in the person's language | **90/90** | 77/90 |
| Median reading grade (English replies) | **5.8** | 7.9 |
| Median words | 61 | 78 |
| Median response time | 11.7 s | 6.1 s |

The guard caught 3 of 90 drafts and the fact-checker fixed all 3. Two promised that pantries "don't ask about immigration status" or income, which nothing in the data supports. The third flagged a phone number, possibly a false alarm, but the rewritten reply was still correct. No reply needed the safe fallback.

Deterministic checks, with no AWS needed and run in CI on every push: 44/44 schedule checks against expected answers worked out by hand from the published hours (first, fifth, and nth-weekday rules, conflicts, unknown times), 5/5 search checks, and 18/18 guard and SMS-cleanup checks. Every reply is in `eval/results.json`.

Notes on fairness and limits:
- Because the baseline's prompt describes tools it doesn't have, it sometimes writes fake tool calls as text. That reflects how a plain chatbot would behave with this prompt, and it is part of why its numbers are low.
- Most checks are automatic string checks, such as whether the right site is named or 911 appears. A reply can pass and still read awkwardly, so I also read the failures and a sample of the passes.
- The unsupported-promise rule only catches the phrasings it knows about, in English and Spanish.
- Hours come from published directories, not calls to each site. The agent always tells people to call first when sources disagree or a time isn't listed.

### Run options

Street addresses and routing also need IAM permission for `geo-places:Geocode` and `geo-routes:CalculateRoutes`.

```
python eval/run_eval.py                                # offline checks, no AWS
python eval/run_eval.py --agent --baseline --runs 3    # live agent vs. no-tools baseline
```

`--now` pins the clock for demos. Leave it off to use the real time in Kalamazoo. Set `FRESHTRACK_MODEL` to use a different Bedrock model, and `FRESHTRACK_OFFLINE=1` to skip AWS (ZIP codes only, straight-line distance).

### Chat on the map

The map page has an **Ask FreshTrack** panel. Replies come from the same agent, and every place a reply names is ringed and labeled on the map. Clicking the map drops a pin, which the agent uses as your location if you don't type one. A demo clock lets you ask "what's open" at any moment. Follow-up questions keep the conversation's context.

Run the map and the agent together locally:

```
python deploy/local_server.py       # then open http://localhost:8000
```

Deploy the agent as a public AWS Lambda Function URL (`lambda/chat.py`):

```
python deploy/deploy.py
```

The deploy script creates or updates four resources named `freshtrack-chat*`: a DynamoDB usage table, an IAM role scoped to the one Bedrock model, Amazon Location, and that table, the Lambda function, and its Function URL. It prints the URL to put in `config.js` as `chatEndpoint`. Because the URL is public and every call costs Bedrock tokens, it is protected by:
- a cap of 500 requests per day, counted atomically in DynamoDB (`--daily-limit`)
- reserved concurrency of 3 (`--concurrency`)
- browser access only from the GitHub Pages site and localhost (CORS)
- message, history, and pin size checks

No message text, location, or IP address is stored. The usage table holds a date and a count.

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
