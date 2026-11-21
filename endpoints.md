# Skool internal endpoint map (verified 2026-06-07)

Skool has no public API. This is the reverse-engineered map our client codes against,
captured from a logged-in browser session and verified live against `solnest-ai`.

- **API host:** `https://api2.skool.com` — auth = session cookie (`auth_token` +
  `aws-waf-token`, both in the Cookie header; the waf cookie clears AWS's firewall).
- **Pages:** `https://www.skool.com/<slug>...` — list views are read from the
  `__NEXT_DATA__` JSON embedded in the page HTML.

## api2.skool.com (cookie auth)

| Capability | Method | Path | Notes |
|---|---|---|---|
| whoami / my groups | GET | `/self/groups?limit=50&prefs=false` | `{groups:[{id,name}]}` |
| post + comments | GET | `/groups/{slug}/post-detail?post={name}` | `{post_tree:{post,...}}`; `name` is the slug |
| comments | GET | `/posts/{postId}/comments?group-id={gid}` | `{post_tree:{children:[...]}}` |
| create post | POST | `/posts?follow=false` | body `{post_type:"post",group_id,metadata:{title,content,...}}` — *inferred, not yet live-tested* |
| create comment | POST | `/posts?follow=false` | body `{post_type:"comment",group_id,root_id,parent_id,metadata:{content,...}}` — verified |
| upvote post | PUT | `/posts/{postId}/vote` | body `{old:"",new:"up"}` (not wired as a tool yet) |
| list conversations | GET | `/self/chat-channels?offset=0&limit=30&last=true&unread-only=false` | `{channels:[...]}` |
| read conversation | GET | `/channels/{channelId}/messages?before={N}` | `before` = how many back from latest |
| send DM | POST | `/channels/{channelId}/messages?ct=wdm` | body `{content,attachments:[]}` — verified |
| mark read | POST | `/channels/{channelId}/read` | verified |
| start new DM | POST | `/users/{userId}/chat-request?g={gid}` | verified |
| course + lessons | GET | `/courses/{courseId}` | `{course,children:[{course}]}`; add `?md={lessonId}` for one lesson |

## www.skool.com `__NEXT_DATA__` (props.pageProps)

| Capability | Path | Data location |
|---|---|---|
| list posts (feed) | `/{slug}` | `pageProps.postTrees` (list of `{post,children}`) |
| list members | `/{slug}/-/members` | `pageProps.users` |
| list courses | `/{slug}/classroom` | `pageProps.allCourses` |

## Admin endpoints (added 2026-11-19, `feat/admin-writes`)

Used by the opt-in admin tools in `admin_tools.py` (writes are off unless `SKOOL_WRITE_TOOLS=1`).
**None of these has been live-tested by this project yet.** The build ran fully offline, by design.
Endpoint facts come from public sources: cotletdev/skool-api-mcp-server (Elastic-2.0, facts only,
no code copied), nklsschroeer707/catknows (MIT), ctala/skool-api-docs (MIT). The per-row source
counts below come from the build brief. This build did not re-read those repos (GitHub access was
blocked in the build session), so treat the attribution as a lead until someone re-checks it.

| Capability | Method | Path | Body | Source | Status |
|---|---|---|---|---|---|
| pending requests | GET (page) | `www.skool.com/{slug}/-/members?t=pending&p={N}` | n/a | brief | list key `users` (may be `null`); membership id = `users[].member.id`, NOT the user id. Answer / risk / timestamp field names unverified, parser tries several and reports `metadata_keys_seen` when it finds none |
| approve member | POST | `/members/{membershipId}/role` | `{"new":"member"}` | multi-source | not live-tested here |
| decline member | POST | `/members/{membershipId}/role` | `{"new":"declined"}` | multi-source | not live-tested here. Same call on an ACTIVE member removes them, so the tool only declines ids in the current pending list AND refuses any id that appears on the active member list (`/{slug}/-/members`, `users[].member.id`). If every decline is refused with "on your ACTIVE member list", that list includes pending requests on your community: report it, do not work around it |
| role already set | (error) | same | n/a | brief | error text contains `cannot update to same role`; treated as already done |
| reply to comment | POST | `/posts?follow=false` | `{post_type:"comment",group_id,root_id:<post>,parent_id:<comment>,metadata:{content,...}}` | same endpoint as the verified comment call | parent = comment is not live-tested here. A 3rd nesting level renders as deleted |
| read one post | GET | `/posts/{id}` | n/a | brief | response wrapping unverified; client accepts `{post:{...}}`, `{post_tree:{post}}` or the bare post |
| edit post / comment | POST | `/posts/{id}/update` | FULL flat `{title,content,attachments,labels,video_links,video_ids}` | live-captured by catknows | PATCH/PUT return 405. Omitted fields are CLEARED, so the client re-fetches and re-sends every field |
| delete post / comment | DELETE | `/posts/{id}` | `{}` (literal, sent as raw bytes) | live-captured by catknows | deletes the post's comments too |
| list events | GET (page) | `www.skool.com/{slug}/calendar` | n/a | brief | `pageProps.renderData.events` (falls back to `pageProps.events`) |
| create event | POST | `/calendar-events` | `{group_id,start_time,end_time,metadata:{title,description,timezone,reminder_disabled:0/1,cover_image:"",location:"<JSON string>",privacy:"<JSON string>"}}` | two sources | `location` e.g. `"{\"location_type\":4,\"location_info\":\"https://...\"}"`, `privacy` e.g. `"{\"privacy_type\":0}"`. location_type: 1 Zoom, 2 Meet, 3 Address, 4 Link, 5 Skool call, 6 Webinar. Title max 30, description max 300. Times sent as UTC `...Z` |
| read event | GET | `/calendar-events/{id}` | n/a | two sources | response wrapping unverified; client accepts `{event:{...}}` or bare |
| update event | PUT | `/calendar-events/{id}` | full object (same shape as create) | two sources | full replace. Client starts from every existing metadata key and overrides only what changed |
| delete event | DELETE | `/calendar-events/{id}` | none | two sources | recurring events refused in v1 |
| invite by email | POST | `/groups/{slug}/invite` | `{"emails":[...],"course_ids":[...]}` | **single source** | **UNVERIFIED.** Skips payment and approval, emails real people, cannot be recalled |

## Known gaps / TODO
- `create_post` (top-level) shape is inferred from the comment body — verify with one real post.
- `solnest-ai` group_id: `<resolved at runtime from SKOOL_GROUP; or set SKOOL_GROUP_ID in .env>`.
- Admin endpoints: first live run should be one approve, one edit, one event create/delete on a
  test community, with the audit log open. Specifically unverified:
  - pending-page field names for join answers, `highRiskScore` and the request time;
  - whether pending entries carry `member.role` (the decline tripwire refuses if any entry on the
    pending page shows an active role like `member`/`admin`);
  - the field that marks a recurring event (detection checks `recurring`, `recurrence`, `rrule`,
    `series_id` and similar, and errs toward refusing);
  - `privacy_type` values other than `0` (everyone);
  - whether the active member list (`/{slug}/-/members`) ever includes pending requests (the
    decline cross-check assumes it does not, and fails closed if it does);
  - whether Skool accepts UTC `Z` times for events (it should: they are valid RFC 3339).
