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

## Known gaps / TODO
- `create_post` (top-level) shape is inferred from the comment body — verify with one real post.
- Member **approve / reject** endpoints not captured (Ryan had no pending members during capture).
  Needs a 30-sec recapture while approving one member.
- `solnest-ai` group_id: `<resolved at runtime from SKOOL_GROUP; or set SKOOL_GROUP_ID in .env>`.
