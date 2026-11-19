"""
Admin tools for a Skool community you run: pending member requests, post and
comment moderation, calendar events, invites.

The two list tools are reads and are always registered. The nine write tools are
registered ONLY when SKOOL_WRITE_TOOLS is on, and every one of them is two-step:

  1. Call with confirm=false (the default). Nothing is written. You get a preview:
     the exact request, the target, warnings, the rate-cap budget, and a one-time
     confirm_token valid for 10 minutes.
  2. Show the preview to the human. Only after they say yes, call again with the
     SAME arguments plus confirm=true and the confirm_token.

The confirm call re-reads the target before writing (write_guard.run), so a
member who was approved in the browser meanwhile, or a post that was deleted, is
caught instead of acted on blind.

HTTP and payload shapes live in skool_client.py; the safety rails in write_guard.py.
"""
from datetime import datetime, timezone as _tz

import skool_client as sk
import write_guard as guard
from write_guard import Plan, WriteRefused

_TWO_STEP = (
    "Two-step: the default confirm=false only returns a preview and a confirm_token. "
    "Show the preview to the user; only after they say yes, call again with the same "
    "arguments plus confirm=true and that confirm_token.")


def _id(value, what):
    try:
        return sk._safe_id(value, what)
    except ValueError as e:
        raise WriteRefused(str(e)) from None


def _get_or_refuse(fetch, ident, what):
    try:
        return fetch(ident)
    except sk.NotFound:
        raise WriteRefused(f"{what} {ident} was not found (already deleted, or a "
                           "wrong id).") from None


# --- reads (always registered) -------------------------------------------------
def skool_list_pending_members(limit: int = 50) -> dict:
    """List pending membership requests in your community (read-only, admins only).
    Returns per request: name, membership_id (the id skool_approve_member and
    skool_decline_member take; it is NOT the user id), join-question answers,
    high_risk_score when Skool provides one, and when they requested."""
    return sk.list_pending_members(limit=limit)


def skool_list_events(limit: int = 50) -> dict:
    """List calendar events in your community (read-only): id, title, start/end,
    timezone, location, privacy, and whether it looks recurring. Use the id with
    skool_update_event or skool_delete_event."""
    return sk.list_events(limit=limit)


# --- members ------------------------------------------------------------------
def _pending_or_refuse(membership_id: str, action: str) -> dict:
    data = sk.pending_members_raw()
    if action == "decline" and data["excluded_active"]:
        raise WriteRefused(
            f"The pending list contained {data['excluded_active']} entries with an "
            "active-member role. That suggests Skool did not apply the pending filter, "
            "and declining an active membership REMOVES the member. Refusing to decline "
            "anything until the pending list looks right (check skool_list_pending_members).")
    for entry in data["pending"]:
        if entry.get("membership_id") == membership_id:
            return entry
    more = ("" if data["complete"] else
            f" Only the first {sk.MAX_PENDING_PAGES} pages of requests were checked.")
    risk = (" Declining a non-pending membership can remove an ACTIVE member, so this "
            "tool only declines current requests." if action == "decline" else "")
    raise WriteRefused(
        f"membership_id {membership_id} is not in the current pending list (already "
        f"approved or declined, or it is a user id instead of a membership id).{risk}{more}")


def _member_plan(membership_id: str, action: str, role: str) -> Plan:
    entry = _pending_or_refuse(membership_id, action)
    who = entry.get("name") or entry.get("handle") or "this member"
    warnings = []
    if action == "decline":
        # Second, independent guard. The pending check above leans on page field
        # names that are not live-verified; this one only needs the ACTIVE list's
        # member ids. Same call on an active membership removes the member, so an
        # id seen there is refused no matter what the pending page said.
        active = sk.active_membership_ids()
        if membership_id in active["ids"]:
            raise WriteRefused(
                f"membership_id {membership_id} is on your ACTIVE member list. Declining "
                "it would remove that member. Refused.")
        if not active["ids"]:
            warnings.append("Could not cross-check against the active member list (it "
                            "showed no membership ids). Make sure this person is a "
                            "pending request, not a member.")
        elif not active["complete"]:
            warnings.append(f"The active-member cross-check only covered the first "
                            f"{active['checked']} members.")
        warnings.append("The person is told their request was declined. There is no "
                        "undo here; they would have to request again.")
    if entry.get("high_risk_score") not in (None, 0, "0"):
        warnings.append(f"Skool flags this request with high_risk_score="
                        f"{entry['high_risk_score']}.")
    return Plan(
        summary=f"{action.capitalize()} {who}'s request to join {sk._group()} "
                f"(membership {membership_id})",
        method="POST",
        url=f"{sk.API}/members/{membership_id}/role",
        body={"new": role},
        targets=[membership_id],
        warnings=warnings,
        details={"member": entry},
    )


def skool_approve_member(membership_id: str, confirm: bool = False,
                         confirm_token: str = "") -> dict:
    """Approve a pending request to join your community. `membership_id` comes from
    skool_list_pending_members (NOT the user id). Refuses if the id is not
    currently pending. If someone already approved it, reports already_done.
    """
    mid = _id(membership_id, "membership_id")
    return guard.run(
        tool="skool_approve_member", bucket="members", group=sk._group(),
        args={"membership_id": mid}, confirm=confirm, confirm_token=confirm_token,
        plan_fn=lambda: _member_plan(mid, "approve", "member"),
        execute_fn=lambda plan: sk.set_member_role(mid, "member"))


def skool_decline_member(membership_id: str, confirm: bool = False,
                         confirm_token: str = "") -> dict:
    """Decline a pending request to join your community. `membership_id` comes from
    skool_list_pending_members (NOT the user id). Only ever declines an id that is
    in the CURRENT pending list: the same call on an active member removes them,
    so anything else is refused.
    """
    mid = _id(membership_id, "membership_id")
    return guard.run(
        tool="skool_decline_member", bucket="members", group=sk._group(),
        args={"membership_id": mid}, confirm=confirm, confirm_token=confirm_token,
        plan_fn=lambda: _member_plan(mid, "decline", "declined"),
        execute_fn=lambda plan: sk.set_member_role(mid, "declined"))


# --- posts and comments ---------------------------------------------------------
def _is_comment(post: dict) -> bool:
    if post.get("post_type") == "comment":
        return True
    root = post.get("root_id")
    return bool(root) and root != post.get("id")


def skool_reply_to_comment(post_id_root: str, parent_comment_id: str, content: str,
                           confirm: bool = False, confirm_token: str = "") -> dict:
    """Reply to a comment, publicly, as you. `post_id_root` is the post's id;
    `parent_comment_id` is the TOP-LEVEL comment you are replying to (ids from
    skool_get_post). To comment on the post itself use skool_comment_on_post.
    WARNING: Skool renders a third nesting level (a reply to a reply) as
    "deleted", so this refuses when the parent is itself a reply.
    """
    root = _id(post_id_root, "post_id_root")
    parent = _id(parent_comment_id, "parent_comment_id")
    text = (content or "").strip()
    if not text:
        raise WriteRefused("content is empty.")
    if root == parent:
        raise WriteRefused("parent_comment_id is the post itself. Use "
                           "skool_comment_on_post to comment on a post.")

    def plan() -> Plan:
        p = _get_or_refuse(sk.get_post_by_id, parent, "Comment")
        warnings = []
        p_root, p_parent = p.get("root_id"), p.get("parent_id")
        if p_root and p_root != root:
            raise WriteRefused(f"Comment {parent} belongs to post {p_root}, not {root}.")
        if p_root and p_parent and p_parent != p_root:
            raise WriteRefused(
                f"Comment {parent} is itself a reply. A reply to it would be a third "
                f"level, which Skool shows as deleted. Reply to the top-level comment "
                f"instead (parent_comment_id={p_parent}).")
        if not p_root:
            warnings.append("Could not verify which post this comment belongs to or its "
                            "nesting level (Skool did not return root_id).")
        md = p.get("metadata") or {}
        return Plan(
            summary=f"Reply publicly to comment {parent} on post {root}",
            method="POST", url=f"{sk.API}/posts?follow=false",
            body=sk.comment_payload(root, parent, text),
            targets=[root, parent], warnings=warnings,
            details={"replying_to": sk._clip(md.get("content"), 300)})

    return guard.run(
        tool="skool_reply_to_comment", bucket="posts", group=sk._group(),
        args={"post_id_root": root, "parent_comment_id": parent, "content": text},
        confirm=confirm, confirm_token=confirm_token, plan_fn=plan,
        execute_fn=lambda p: sk.reply_to_comment(root, parent, text))


def skool_edit_post(post_id: str, title: str | None = None, content: str | None = None,
                    confirm: bool = False, confirm_token: str = "") -> dict:
    """Edit a post's title and/or content (also works on a comment: pass the
    comment id and content). Leave a field out to keep it. Skool replaces the
    whole post on edit, so this re-fetches the post and re-sends every other field
    (attachments, labels, videos) unchanged; the preview lists anything it could
    not find, which would otherwise be cleared.
    """
    pid = _id(post_id, "post_id")
    if title is None and content is None:
        raise WriteRefused("Nothing to change: pass title and/or content.")

    def plan() -> Plan:
        cur = _get_or_refuse(sk.get_post_by_id, pid, "Post")
        md = cur.get("metadata") or {}
        body = sk.build_post_update(cur, title=title, content=content)
        changed = [f for f in ("title", "content") if body[f] != (md.get(f) or "")]
        if not changed:
            raise WriteRefused("The new values match the current post; nothing to edit.")
        warnings = []
        missing = [f for f in sk.POST_UPDATE_FIELDS
                   if f not in md and f not in ("title", "content")]
        if missing:
            warnings.append(
                f"The fetched post has no {missing} field(s), so they will be sent empty. "
                "If this post visibly has attachments, labels or videos, STOP: they would "
                "be cleared.")
        if _is_comment(cur) and title:
            warnings.append("This is a comment; comments normally have no title.")
        return Plan(
            summary=f"Edit {'comment' if _is_comment(cur) else 'post'} {pid} "
                    f"({', '.join(changed)})",
            method="POST", url=f"{sk.API}/posts/{pid}/update", body=body,
            targets=[pid], warnings=warnings,
            details={"current": {"title": md.get("title"),
                                 "content": sk._clip(md.get("content"), 1500)},
                     "changing": changed})

    return guard.run(
        tool="skool_edit_post", bucket="posts", group=sk._group(),
        args={"post_id": pid, "title": title, "content": content},
        confirm=confirm, confirm_token=confirm_token, plan_fn=plan,
        execute_fn=lambda p: {"status": "ok", "response": sk.update_post(pid, p.body)})


def skool_delete_post(post_id: str, confirm: bool = False, confirm_token: str = "") -> dict:
    """Delete a post or a comment. Deleting a post deletes ALL of its comments;
    deleting a comment may also delete its replies. Cannot be undone from here.
    """
    pid = _id(post_id, "post_id")

    def plan() -> Plan:
        cur = _get_or_refuse(sk.get_post_by_id, pid, "Post")
        md = cur.get("metadata") or {}
        kind = "comment" if _is_comment(cur) else "post"
        label = md.get("title") or sk._clip(md.get("content"), 80) or "(no text)"
        warning = ("Deleting a post deletes ALL of its comments." if kind == "post"
                   else "Deleting a comment may also delete its replies (not verified).")
        return Plan(
            summary=f"Delete {kind} {pid}: {label!r}"
                    + (f" ({md.get('comments')} comments)" if md.get("comments") else ""),
            method="DELETE", url=f"{sk.API}/posts/{pid}", body={},
            targets=[pid],
            warnings=[warning, "This cannot be undone from here."],
            details={"title": md.get("title"), "content": sk._clip(md.get("content"), 600),
                     "comments": md.get("comments")})

    return guard.run(
        tool="skool_delete_post", bucket="posts", group=sk._group(),
        args={"post_id": pid}, confirm=confirm, confirm_token=confirm_token,
        plan_fn=plan,
        execute_fn=lambda p: {"status": "ok", "response": sk.delete_post(pid)})


# --- calendar -------------------------------------------------------------------
_RECURRING_REFUSAL = (
    "This looks like a recurring event. Changing or deleting recurring events "
    "('this one' vs 'this and following') is not supported in this version: do it in "
    "the Skool calendar UI.")
_RECURRING_UNVERIFIED = (
    "Recurring detection is best-effort (the field names are not live-verified). If "
    "this event repeats, STOP and do it in the Skool UI instead.")


def _utc(value):
    try:
        s = str(value).strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.astimezone(_tz.utc) if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def _duplicate_event(title: str, start_iso: str):
    """An existing event with the same title and start: the classic double-create
    from a retried call. None if none, or if the calendar could not be read."""
    try:
        events = sk.list_events(limit=200).get("events", [])
    except Exception:
        return "unknown"
    want = _utc(start_iso)
    for ev in events:
        if (ev.get("title") or "").strip().lower() != title.strip().lower():
            continue
        have = _utc(ev.get("start_time"))
        if (want and have and want == have) or ev.get("start_time") == start_iso:
            return ev
    return None


def skool_create_event(title: str, start_time: str, end_time: str, timezone: str,
                       location_type: int, location_info: str = "",
                       description: str = "", privacy_type: int = 0,
                       reminder_disabled: bool = False, confirm: bool = False,
                       confirm_token: str = "") -> dict:
    """Create a calendar event in your community.
    title max 30 characters; description max 300. start_time/end_time are ISO 8601
    WITH a UTC offset, e.g. 2026-12-01T18:00:00-08:00 (sent to Skool as UTC).
    timezone is an IANA name, e.g. America/Vancouver. location_type: 1 Zoom,
    2 Google Meet, 3 Address, 4 Link, 5 Skool call, 6 Webinar; location_info is
    the link or address (required for 1-4). privacy_type 0 = everyone in the group.
    Refuses if an event with the same title and start already exists.
    """
    args = {"title": title, "start_time": start_time, "end_time": end_time,
            "timezone": timezone, "location_type": location_type,
            "location_info": location_info, "description": description,
            "privacy_type": privacy_type, "reminder_disabled": bool(reminder_disabled)}

    def plan() -> Plan:
        payload = sk.build_event_payload(
            sk._group_id(), title, description, start_time, end_time, timezone,
            location_type, location_info, privacy_type, reminder_disabled)
        md = payload["metadata"]
        dup = _duplicate_event(md["title"], payload["start_time"])
        warnings = []
        if dup == "unknown":
            warnings.append("Could not read the calendar to check for a duplicate event.")
        elif dup:
            raise WriteRefused(f"An event titled {md['title']!r} already starts at "
                               f"{payload['start_time']} (id {dup.get('id')}). Not "
                               "creating a duplicate.")
        if not reminder_disabled:
            warnings.append("Skool may send event reminders to members "
                            "(reminder_disabled=false).")
        return Plan(
            summary=f"Create event {md['title']!r} {payload['start_time']} to "
                    f"{payload['end_time']} ({md['timezone']}, "
                    f"{sk.LOCATION_TYPES[int(location_type)]}) in {sk._group()}",
            method="POST", url=f"{sk.API}/calendar-events", body=payload,
            targets=[md["title"]], warnings=warnings)

    return guard.run(
        tool="skool_create_event", bucket="calendar", group=sk._group(), args=args,
        confirm=confirm, confirm_token=confirm_token, plan_fn=plan,
        execute_fn=lambda p: {"status": "ok", "response": sk.create_event(p.body)})


def skool_update_event(event_id: str, title: str | None = None,
                       description: str | None = None, start_time: str | None = None,
                       end_time: str | None = None, timezone: str | None = None,
                       location_type: int | None = None, location_info: str | None = None,
                       privacy_type: int | None = None,
                       reminder_disabled: bool | None = None, confirm: bool = False,
                       confirm_token: str = "") -> dict:
    """Change a calendar event. Pass only what changes; everything else is kept.
    Skool replaces the whole event on update, so this re-fetches it and re-sends
    every field. Same formats and limits as skool_create_event. Recurring events
    are refused (use the Skool UI for those).
    """
    eid = _id(event_id, "event_id")
    changes = {"title": title, "description": description, "start_time": start_time,
               "end_time": end_time, "timezone": timezone, "location_type": location_type,
               "location_info": location_info, "privacy_type": privacy_type,
               "reminder_disabled": reminder_disabled}
    if all(v is None for v in changes.values()):
        raise WriteRefused("Nothing to change: pass at least one field.")

    def plan() -> Plan:
        cur = _get_or_refuse(sk.get_event, eid, "Event")
        if sk.looks_recurring(cur):
            raise WriteRefused(_RECURRING_REFUSAL)
        payload = sk.build_event_update(
            cur, sk._group_id(), title=title, description=description,
            start_time=start_time, end_time=end_time, timezone_name=timezone,
            location_type=location_type, location_info=location_info,
            privacy_type=privacy_type, reminder_disabled=reminder_disabled)
        before = sk.event_summary(cur)
        after = sk.event_summary({**cur, **payload})
        return Plan(
            summary=f"Update event {eid} {before.get('title')!r} "
                    f"({', '.join(k for k, v in changes.items() if v is not None)})",
            method="PUT", url=f"{sk.API}/calendar-events/{eid}", body=payload,
            targets=[eid], warnings=[_RECURRING_UNVERIFIED],
            details={"before": before, "after": after})

    return guard.run(
        tool="skool_update_event", bucket="calendar", group=sk._group(),
        args={"event_id": eid, **changes}, confirm=confirm, confirm_token=confirm_token,
        plan_fn=plan,
        execute_fn=lambda p: {"status": "ok", "response": sk.update_event(eid, p.body)})


def skool_delete_event(event_id: str, confirm: bool = False, confirm_token: str = "") -> dict:
    """Delete a single (non-recurring) calendar event. Recurring events are refused:
    use the Skool UI for those. Cannot be undone from here.
    """
    eid = _id(event_id, "event_id")

    def plan() -> Plan:
        cur = _get_or_refuse(sk.get_event, eid, "Event")
        if sk.looks_recurring(cur):
            raise WriteRefused(_RECURRING_REFUSAL)
        s = sk.event_summary(cur)
        return Plan(
            summary=f"Delete event {eid} {s.get('title')!r} starting {s.get('start_time')}",
            method="DELETE", url=f"{sk.API}/calendar-events/{eid}", body=None,
            targets=[eid],
            warnings=["This cannot be undone from here.", _RECURRING_UNVERIFIED],
            details={"event": s})

    return guard.run(
        tool="skool_delete_event", bucket="calendar", group=sk._group(),
        args={"event_id": eid}, confirm=confirm, confirm_token=confirm_token,
        plan_fn=plan,
        execute_fn=lambda p: {"status": "ok", "response": sk.delete_event(eid)})


# --- invites --------------------------------------------------------------------
INVITE_WARNINGS = [
    "An invite lets these people in directly: it SKIPS payment and the approval "
    "questions.",
    "Skool emails real people at these addresses, from your community.",
    "An invite cannot be recalled from here once sent.",
    "UNVERIFIED endpoint: documented by a single public source and never live-tested "
    "by this project. Check the result in Skool afterwards.",
]


def skool_invite_members(emails: list[str], course_ids: list[str] | None = None,
                         confirm: bool = False, confirm_token: str = "") -> dict:
    """Invite people to your community by email (optionally unlocking courses by
    id from skool_list_courses). Max 5 emails per call and 20 per day by default.
    An invite SKIPS payment and approval, emails real people, and cannot be
    recalled. Already-members are refused when the member list shows emails.
    UNVERIFIED endpoint (single public source, not live-tested).
    """
    valid, invalid = sk.normalize_emails(emails)
    if invalid:
        raise WriteRefused(f"Not valid email addresses: {invalid}")
    if not valid:
        raise WriteRefused("No email addresses given.")
    per_call = guard.invites_per_call()
    if len(valid) > per_call:
        raise WriteRefused(f"{len(valid)} emails; the limit is {per_call} per call "
                           "(SKOOL_CAP_INVITES_PER_CALL). Split them up.")
    cids = sorted({_id(c, "course_id") for c in (course_ids or [])})

    def plan() -> Plan:
        known = sk.member_emails()
        already = [e for e in valid if e in known["emails"]]
        if already:
            raise WriteRefused(f"Already members, not inviting: {already}")
        warnings = list(INVITE_WARNINGS)
        if not known["any_emails_visible"]:
            warnings.append("Could not check for existing members: the member list did "
                            "not include email addresses.")
        elif not known["complete"]:
            warnings.append(f"The existing-member check only covered the first "
                            f"{known['checked']} members.")
        if cids:
            existing = sk.course_ids()
            unknown = [c for c in cids if c not in existing]
            if unknown:
                raise WriteRefused(f"Unknown course ids: {unknown} (see skool_list_courses).")
        return Plan(
            summary=f"Invite {len(valid)} people to {sk._group()}"
                    + (f" with {len(cids)} course(s) unlocked" if cids else ""),
            method="POST", url=f"{sk.API}/groups/{sk._group()}/invite",
            body={"emails": valid, "course_ids": cids},
            targets=valid, units=len(valid), warnings=warnings)

    return guard.run(
        tool="skool_invite_members", bucket="invites", group=sk._group(),
        args={"emails": valid, "course_ids": cids}, confirm=confirm,
        confirm_token=confirm_token, plan_fn=plan,
        execute_fn=lambda p: {"status": "ok", "response": sk.invite_members(valid, cids)})


# --- registration -----------------------------------------------------------------
READ_TOOLS = (skool_list_pending_members, skool_list_events)
WRITE_TOOLS = (skool_approve_member, skool_decline_member, skool_reply_to_comment,
               skool_edit_post, skool_delete_post, skool_create_event,
               skool_update_event, skool_delete_event, skool_invite_members)

# The MCP SDK takes each tool's description from __doc__ at registration. A
# `"""...""" + X` expression is NOT a docstring (only a bare literal is), so the
# shared two-step instructions are appended here instead.
for _fn in WRITE_TOOLS:
    _fn.__doc__ = (_fn.__doc__ or "").rstrip() + "\n" + _TWO_STEP
del _fn


def register(mcp, writes: bool | None = None) -> list:
    """Register the admin tools on an MCP server. Write tools only when the switch
    is on, so with it off an agent cannot even see them. Returns the names."""
    if writes is None:
        writes = guard.writes_enabled()
    names = []
    for fn in READ_TOOLS + (WRITE_TOOLS if writes else ()):
        mcp.tool()(fn)
        names.append(fn.__name__)
    return names
