# FB Daily

Human-like **daily activity** on Facebook Lite across all your USB phones, to keep
accounts looking organic (and avoid the "confirm you're human" / suspension flags
that come from aggressive, perfectly-synchronized automation).

Each phone runs **on its own random schedule** — staggered starts, random scroll
speeds, random pauses.

## 🖱️ The app (easy mode — no terminal)
Double-click **`Start FB Daily.bat`** (or run `python gui.py`). A window opens with:
- your connected **phones** and their live status,
- one big **START / STOP** button (STOP closes FB Lite on every phone),
- live **counters** (reactions, comments, shares, chat replies, friend requests),
- a scrolling **activity feed**, and
- simple **settings** — toggles for *Reply to messages / Add friends / React·comment·share*,
  a **Chat tone** picker (friendly / flirty / nice / playful / sweet / chill), and an
  **Intensity** selector (Gentle / Normal / Active).

**Branding:** save your icon as **`fbdaily/icon.png`** and it's used for the splash
and window (a Facebook-blue badge is drawn as a fallback until then). The splash and
theme match the icon — glossy Facebook blue with a playful Tom-chases-Jerry loader.

### Standalone .exe (no Python needed)
A packaged app is built at **`fbdaily/dist/FB Daily.exe`** — double-click it, no
Python required. It bundles `adb` so it's portable (you can move/share the single
`FB Daily.exe`). To rebuild (e.g. after adding `icon.png` / `icon.ico` for full
branding):
```
python -m PyInstaller --noconfirm --distpath fbdaily/dist --workpath fbdaily/build fbdaily/FBDaily.spec
```
(Add `fbdaily/icon.ico` before rebuilding to set the exe's own file/taskbar icon.)

## Or run it from the terminal — no arguments
```
python fbdaily.py
```

It **runs forever** — session after session on every phone — until you stop it.
It won't quit on its own: if a step fails it just moves on to the next session.

**To stop:**
- press **Ctrl+C** in its window, **or**
- create a file named **`STOP`** in this folder (works even when it's running in
  the background / detached): on Windows, `type nul > STOP` from this folder.

When you stop it, **every phone finishes its current step, then closes FB Lite**
and returns to its home screen — the activity fully ends on all phones.

It clears any leftover `STOP` file when it starts, and removes it again when it
stops, so the next run isn't blocked.

Every USB-connected phone automatically does its daily activity, in a
randomized order, all at its own pace:

- **Confirm friend requests** (any incoming), then **add friends** (a random few
  from "People you may know").
- **Reply to messages** — if someone messaged the account, a free AI writes a
  **context-aware reply** (based on what they said) and sends it.
- **Browse its own feed** — scroll, watch reels, check notifications, open posts.
- **Engage the timeline posts it scrolls past** — a randomized **reaction**, and
  sometimes a **comment** and/or a **share**. There is **no fixed target post**;
  it acts on whatever is in that account's feed.

There are no command-line flags. Everything is tuned through `config.json`.

## Messenger auto-reply (free AI, real conversations) — PRIORITIZED
**Messages come first.** Each session it answers all pending messages *before*
friends/feed, and while it's browsing it **re-checks Messenger every
`check_interval_seconds`** (default 60) and jumps on any new message or reply
before continuing. So new messages/replies are always handled first.

When people message the account, it **chats back based on what they said**, using
the free **Pollinations** AI (same one the comments use — no key needed). It:
- opens Messenger and finds **every unread conversation** (the ones with a red
  badge),
- opens each, **reads the whole thread**, and writes a natural next reply that
  **continues the conversation** in a chosen **tone**,
- and because their next reply arrives as a new unread, the conversation **keeps
  building** across sessions — on **every** phone.

Unlike the news feed, FB Lite's **Messenger is accessible**, so it reads the real
messages (no notification/preview dependency) and taps the real "Send message"
button.

Tune it under `chat` in `config.json`:
- `enabled` — on/off
- `persona` — who the account is (e.g. "a busy online seller")
- `styles` — the tones it randomly uses (steady per person), e.g.
  `["friendly and warm", "nice and polite", "playful and fun", "sweet",
  "a little flirty", "chill and casual"]`
- `max_per_session` — how many conversations to answer per session (default 5)

## USB only
Acts **only on phones connected by USB** with ADB. Wireless ADB targets
(serials like `192.168.x.x:5555`) are ignored on purpose.

Requirements per phone: USB debugging on + this computer authorized, Facebook
Lite installed and logged in, screen lock off or swipe-only (no PIN).

## What each phone does
All engagement is on **feed / timeline posts** (no configured URL).
- **Reactions** are weighted-random (like/love/haha common, angry rare) so they
  look human, not uniform. Reliable.
- **Comments** reuse the reactor's AI pipeline (`../fb.py` → `unique_comment`) —
  throttled and unique per account; if `engage.comment_example` is blank they
  come from a built-in local pool. To comment/share it opens the post's comment
  thread (a detail view) first; on posts where that fails it just leaves the
  reaction, so comment/share are best-effort per post.
- **Share** is **best-effort** — FB Lite's Share button has no accessibility
  label and moves around per post (some posts don't show it), so it lands on
  some posts and is skipped on others.
- **Add friends / confirm requests** — the Friends UI has no accessibility
  labels either, so these are coordinate-based (the blue Add friend / Confirm
  button is the left button of each row, ~0.49×width). Add-friend is reliable;
  confirm works when a request is present, and otherwise harmlessly lands on a
  suggestion.
- **Feed engagement is self-correcting** — a blind feed tap may open a photo, a
  profile, or a link, so it detects what opened and only reacts on a real post
  surface, otherwise backs out. It never fires blindly.
- **Stay-in-app watchdog** — before every activity it checks the foreground app;
  if a tapped link opened a browser, another app came up, or it drifted to the
  home screen, it backs out / relaunches **FB Lite** and continues.

## Cadence
It runs continuously with a short random gap between sessions
(`between_sessions_seconds`). Leave it running as long as you want; stop it with
Ctrl+C or the `STOP` file.

## Tuning — `config.json`
- `session_minutes` / `stagger_seconds` — feed-browsing length and random start delay
- `between_sessions_seconds` — random pause between one session and the next
- `read_pause_seconds` / `watch_pause_seconds` — human pause ranges
- `weights` — how often each feed activity runs (`scroll`, `reels`, `engage`,
  `add_friends`, `confirm_requests`, …). Raise `engage` for more
  reactions/comments/shares on timeline posts.
- `engage`:
  - `react_prob` / `share_prob` / `comment_prob` — chance of each per feed engagement
  - `comment_example` — example comment the AI varies (blank = local pool)
  - `reaction_weights` — relative frequency of each reaction type
  - `add_friends_range` / `confirm_range` — how many per session
- `loop` / `loop_gap_hours` — daily repeat + gap

## Notes / limits
- Gesture/coordinate-based; tuned for 720-wide screens (TECNO KM4 / itel
  A6611L). Nav / friend / share taps use screen fractions — adjust for very
  different screens.
- **Keep it modest.** Mass friend-adding and rapid identical activity are spam
  signals; a couple of accounts got flagged from heavy runs. The randomized,
  staggered defaults are deliberately gentle — keep them that way.
