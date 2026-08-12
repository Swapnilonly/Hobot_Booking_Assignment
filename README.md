# HabotConnect — LSA Booking Backend Prototype

**Candidate:** [Your Full Name] — [Your Phone] — [Your Email] — [Your GitHub]
> Fill in the line above before submitting. Every deliverable (repo, slides, docs) must be labeled with your name and contact info per the assignment brief.

A production-style backend prototype for HabotConnect's core booking flow: parents search for
available Learning Support Assistants (LSAs), book a session, pay for it, and the system
reacts to payment webhook events to confirm or reject the booking.

## 1. Architecture Overview: MVC vs MVT

Django follows the **MVT (Model-View-Template)** pattern, not classic MVC:

| Classic MVC | Django MVT | Role in this project |
|---|---|---|
| Model | Model | `bookings/models.py` — schema, indexes, constraints |
| Controller | View | `bookings/views.py` — thin, HTTP-in/HTTP-out only |
| View | Template | N/A here — this is a pure JSON API (DRF renders the response) |

The key distinction: in MVC the **Controller** owns business logic and orchestrates the Model/View.
In Django's MVT, Django itself is the controller (URL routing + request/response cycle) — the
**View** is closer to MVC's controller, and there is no template layer needed for a JSON API.

**Design choice I made on top of MVT:** views should stay thin. Business logic (overlap
checking, N+1-safe search, webhook state transitions) lives in a dedicated **service layer**
(`bookings/services.py`), not in the views or serializers. This keeps views testable at the
HTTP boundary and services testable at the logic boundary, independently. It also means the
same booking logic can be reused from a management command, Celery task, or admin action
without duplicating it in a view.

```
Request → View (thin, validates input via Serializer) → Service (business rules, DB transaction)
→ Model (persistence, constraints) → Response
```

## 2. Database Schema

```
User (Django auth) ──1:1── Parent
User (Django auth) ──1:1── LSAProfile ──M2M── Skill
Parent ──1:N── BookingRequest ──N:1── LSAProfile
BookingRequest ──1:1── Payment
```

- **Parent**: linked to Django's `User` for auth; stores contact info.
- **LSAProfile**: linked to `User`; `is_available` flag + M2M `Skill` for search filtering.
- **BookingRequest**: the core entity. `status` is a state machine
  (`pending_payment → confirmed | payment_failed`, plus `cancelled`/`completed`).
- **Payment**: 1:1 with `BookingRequest`; `transaction_id` is unique and is the webhook's
  correlation key.

### Indexing decisions
- `idx_lsa_time_range` — composite index on `(lsa, start_time, end_time)` on `BookingRequest`.
  Every overlap check filters by `lsa` first then range-compares times, so this composite
  index lets Postgres/MySQL do the overlap lookup via an index scan instead of a full table scan.
- `status` index on `BookingRequest` — dashboards/queues will frequently filter by status.
- `transaction_id` index on `Payment` — the webhook's only lookup key, must be O(log n).
- `is_available` index on `LSAProfile` — the search endpoint's most common filter.
- A DB-level `CheckConstraint` (`end_time > start_time`) as a second line of defense
  in case a row is ever inserted outside the service layer (bulk import, admin, migration).

## 3. Solving the N+1 Problem (`GET /api/v1/lsas/search/`)

Naively serializing each LSA's skills (`lsa.skills.all()` inside a loop) issues 1 query for
the LSA list + 1 query per LSA for its skills = **N+1 queries**.

`LSASearchService.search()` uses `select_related('user')` (single JOIN for the 1:1 user) and
`prefetch_related('skills')` (one extra batched query for *all* related skills across *all*
LSAs in the queryset). Total: **2 queries, regardless of how many LSAs match** — verified in
`test_search_query_count_stays_constant_regardless_of_lsa_count`, which asserts an exact query
count using `django_assert_num_queries`.

## 4. Poka-Yoke: Preventing Double-Bookings

Two independent layers, so a mistake at one layer can't corrupt data:

1. **Application layer** (`BookingService.has_overlap`): a single indexed query using interval-
   overlap logic (`a_start < b_end AND b_start < a_end`), evaluated in the database — not by
   loading every booking into Python and looping.
2. **Concurrency safety**: `select_for_update()` locks the LSA row for the duration of the
   transaction, closing the classic check-then-act race condition where two simultaneous
   requests could both pass the overlap check before either commits.
3. **Database layer**: a `CheckConstraint` guards against `end_time <= start_time` regardless
   of entry point.

## 5. Payment Webhook & Idempotency (`POST /api/payments/webhook/`)

Payment gateways commonly **retry** webhook delivery (network blips, timeouts). If the same
`payment.success` event is delivered twice, the second delivery must be a safe no-op — it must
not, for example, re-confirm an already-cancelled booking or double-count anything.
`PaymentWebhookService.process_event` checks the payment's *current* status before mutating it;
if it's already in the target state, it logs and returns without touching the booking again.
Covered by `test_webhook_is_idempotent_on_replay`.

## 6. API Reference

### `POST /api/v1/bookings/`
Creates a booking in `pending_payment` state.

```json
// Request
{
  "parent_id": 1,
  "lsa_id": 3,
  "start_time": "2026-08-15T10:00:00Z",
  "end_time": "2026-08-15T11:00:00Z",
  "notes": "First session, please introduce gently."
}
```
| Status | Meaning |
|---|---|
| `201` | Booking created, `status: "pending_payment"` |
| `400` | Overlapping slot, invalid time range, or missing/invalid `parent_id` / `lsa_id` |

### `GET /api/v1/lsas/search/?skills=Dyslexia Support,ADHD Coaching`
Returns available LSAs, optionally filtered by skill name(s) (comma-separated, OR match).
Omit `skills` to list all available LSAs.

### `POST /api/payments/webhook/`
Mock payment-gateway callback.
```json
{
  "transaction_id": "txn_abc123",
  "event_type": "payment.success",
  "amount": "500.00"
}
```
`event_type` is `payment.success` or `payment.failed`. Transitions the linked booking to
`confirmed` or `payment_failed` respectively. Idempotent on replay.

## 7. Setup Instructions

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/

python manage.py runserver
```

### Running tests
```bash
pytest -v
```
12 tests covering success paths, overlap edge cases (including the adjacent-slot boundary
and cancelled-booking non-blocking case), and failure paths (bad input, unknown transaction).

### Switching to PostgreSQL for production
Default is SQLite for zero-friction local setup. For production, swap `DATABASES` in
`settings.py` to `django.db.backends.postgresql` — the composite indexes and `CheckConstraint`
translate directly; no schema logic needs to change.

## 8. What I'd Add With More Time
- Real JWT auth (`request.user` → `Parent`/`LSAProfile`, instead of `parent_id` in the payload).
- Real payment gateway signature verification on the webhook (HMAC, per gateway docs).
- Celery for any async side-effects (booking confirmation emails/SMS).
- Rate limiting on the search endpoint.
