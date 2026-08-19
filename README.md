# Smart Blood Donation System (HemoLink)

A Django application that connects blood donors and patients by **precise map
location**, **transfusion compatibility** and **AI-predicted willingness to donate**.

Runs on Django + the Python standard library only — no numpy, no scikit-learn, no
PostGIS, no CSS framework.

## The flow

1. **Donors build a rich profile** and drop a pin on the map at their exact location
   (click, drag, "use my location", or place-name search).
2. **A patient registers** and pins their own location.
3. **They search the map in real time** — filtering by radius, blood group,
   availability and verification. Results refresh as filters change, as the search
   centre is dragged, and on a polling timer.
4. **The AI ranks every compatible donor** by predicted probability of accepting,
   and explains each score.
5. **The patient sends the request** to chosen donors, who accept or decline from
   their inbox. Contact details unlock only on acceptance.

## Quick start

```bash
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_demo           # 60 donors, 6 patients, 1 hospital, AI training data
python manage.py train_ranker --force
python manage.py runserver
```

Demo logins (password `demo12345`): `demo_donor1`, `demo_recipient1`, `demo_hospital`.

Create an admin with `python manage.py createsuperuser`.

## How the ranking model works

A **logistic regression** over 14 normalised features predicting
`P(donor accepts | features)`. See `matching/ranking.py`.

| Group | Features |
|---|---|
| Geospatial | proximity, within travel limit, urgency×proximity |
| Medical | exact group match, preserves rare universal stock, readiness (cool-down) |
| Behavioural | acceptance rate, completion rate, response speed, experience, no-show penalty |
| Trust / liveness | verified identity, recent activity, reachable now |

Three design decisions worth knowing about:

- **Cold start.** Before any real data exists, hand-tuned domain priors supply the
  weights, scaled to a fixed budget so scores spread across 0–1 instead of
  saturating. A fresh install ranks sensibly on day one.
- **It learns from use.** Every invitation stores the feature vector that produced
  its score; when the donor answers, that becomes a labelled example. Once enough
  new labels accumulate, weights are refit by gradient descent with L2
  regularisation, evaluated on a 25% holdout, and published as a new version.
  Rollback is a single `is_active` flip.
- **Monotonic sign constraints.** Features whose direction is known a priori (a
  closer donor is never worse; a no-show history is never better) are projected
  back onto the correct side of zero after each gradient step. On small datasets
  this measurably improves generalisation — in the seeded demo it lifted holdout
  AUC from 0.810 to 0.857 — and it prevents absurd explanations such as
  *"Caution: usually accepts requests"*.

Every score is auditable: `/matching/insights/` shows the live weights, holdout
metrics and version history.

## Geo search without PostGIS

Proximity search runs in two stages (`core/geo.py`):

1. An **indexed bounding-box** filter the database answers using a composite index
   on `(latitude, longitude)`, discarding almost every row.
2. An exact **haversine** pass in Python over the small survivor set, which trims
   the box corners and orders by true distance.

The bounding box is deliberately widened by `1/cos(latitude)` so it can never clip
a donor who is genuinely in range; a test walks the compass at the radius to prove it.

## Safety and privacy

- ABO/Rh compatibility is derived from a single source table, so the donor→recipient
  and recipient→donor directions cannot drift apart.
- The 90-day cool-down, age (18–65) and weight (≥50 kg) limits are enforced in
  forms, in queries and at dispatch time. A donor who donates is automatically
  moved to `RESTING` and disappears from searches.
- Browsing the map **never** exposes a phone number, email or street address; the
  search API omits those fields entirely. Contact details unlock only for a
  requester whose invitation the donor accepted. Tests assert this.
- Role-based access control on every view; object-level checks scope invitations
  and requests to their owner. A donor cannot answer someone else's invitation.
- Only the requester can confirm a donation, so donors cannot inflate their own
  reliability record — which keeps the training signal trustworthy.

## Layout

```
core/            geo, compat, eligibility, abstract models, form mixins, decorators
accounts/        CustomUser, auth, role dashboards
donors/          DonorProfile (geo-located), donation records, inbox
recipients/      RecipientProfile
hospitals/       HospitalProfile, blood inventory
blood_requests/  BloodRequest, DonorRequest (invitation + ML training row), services
matching/        ranking engine, search services, live map APIs, RankingModel
notifications/   in-app notification feed
static/          design-system CSS, map picker JS, live search map JS
templates/       full UI
```

## Commands

```bash
python manage.py seed_demo [--donors N] [--reset]
python manage.py train_ranker [--force]
python manage.py run_housekeeping     # expire stale invitations/requests; cron-friendly
python manage.py test
```

## Tests

```bash
python manage.py test          # 266 tests
```

Coverage includes haversine against known distances, bounding-box completeness,
the full compatibility matrix, eligibility edge cases, model calibration, sign
constraints, training and metrics (AUC/log-loss), the complete request lifecycle,
privacy leakage assertions, access control, and an end-to-end journey test
(`tests_journey.py`) that walks registration → map pin → live search → AI ranking →
invitation → acceptance → confirmation entirely over HTTP.

## Notes and limitations

- "Real time" is short-interval polling, not WebSockets. This keeps the deployment
  to plain Django with no broker; `matching/services.py` is the seam to swap in
  Channels later.
- Avatars use `FileField` with an extension whitelist rather than `ImageField`,
  which would require Pillow.
- Place-name search calls the public Nominatim service and degrades gracefully to
  manual pinning if it is unavailable.
- Notifications are in-app only; no email/SMS gateway is wired up.
- For production, set `SBDS_SECRET_KEY`, `SBDS_DEBUG=false` and `SBDS_ALLOWED_HOSTS`.
