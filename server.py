#!/usr/bin/env python3
"""
Skool MCP server — gives Claude full access to your Skool community.

Thin by design: each tool delegates to skool_client.py, which holds the auth +
HTTP logic. Auth is your session cookie in .env (see README / connect_skool.py).
"""
# The MCP SDK renamed this class in 2.0 (FastMCP -> MCPServer) and dropped the old
# import path entirely. Both expose the same .tool() decorator and .run(), so try the
# current path first and fall back for anyone on a 1.x environment.
try:                                      # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:                       # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

import skool_client as sk

mcp = MCPServer("skool")


# --- identity ---------------------------------------------------------------
@mcp.tool()
def skool_whoami() -> dict:
    """Confirm the Skool cookie works and list the communities you belong to.
    Call this first if other tools error — it isolates auth/cookie problems."""
    return sk.whoami()


# --- feed / posts -----------------------------------------------------------
@mcp.tool()
def skool_list_posts(limit: int = 20) -> dict:
    """List recent posts in your community feed (read-only). Returns each post's
    id, `name` (slug), title, a content snippet, comment/upvote counts, author.
    Use the `name` with skool_get_post to read a full post + its comments."""
    return sk.list_posts(limit=limit)


@mcp.tool()
def skool_get_post(post_name: str, with_comments: bool = True) -> dict:
    """Read one full post and (by default) its comments. `post_name` is the slug
    from skool_list_posts (the `name` field, e.g. 'big-change-...'), not the id."""
    return sk.get_post(post_name, with_comments=with_comments)


@mcp.tool()
def skool_create_post(title: str, content: str) -> dict:
    """Publish a NEW top-level post to your community feed. Writes publicly —
    confirm wording with the user first. Returns the new post id."""
    return sk.create_post(title=title, content=content)


@mcp.tool()
def skool_comment_on_post(post_id: str, content: str) -> dict:
    """Add a comment to a post. `post_id` is the real post id (the `id` field from
    skool_list_posts/skool_get_post, not the slug). Writes publicly — confirm first."""
    return sk.comment_on_post(post_id=post_id, content=content)


# --- members ----------------------------------------------------------------
@mcp.tool()
def skool_list_members(limit: int = 30) -> dict:
    """List members of your community (read-only): id, name, email, short bio.
    Use member ids with skool_start_chat to open a DM."""
    return sk.list_members(limit=limit)


# --- classroom --------------------------------------------------------------
@mcp.tool()
def skool_list_courses() -> dict:
    """List the classroom courses in your community (read-only): id, title,
    description. Use a course id with skool_get_lesson to read its lessons."""
    return sk.list_courses()


@mcp.tool()
def skool_get_lesson(course_id: str, lesson_id: str = "") -> dict:
    """Read a classroom course (read-only). `course_id` from skool_list_courses
    returns the course's modules/lessons (id + title). Pass a `lesson_id` (a child
    id) to also pull that lesson's content/metadata (note: many lessons are video-
    only, so the text `content` may be empty)."""
    return sk.get_lesson(course_id=course_id, lesson_id=lesson_id)


# --- DMs / inbox ------------------------------------------------------------
@mcp.tool()
def skool_list_conversations(limit: int = 30) -> dict:
    """Your DM inbox (read-only): conversations with channel_id, unread count, and
    last-message preview. Use a channel_id with skool_get_conversation to read it."""
    return sk.list_conversations(limit=limit)


@mcp.tool()
def skool_get_conversation(channel_id: str, limit: int = 30) -> dict:
    """Read the messages in one DM conversation (read-only). Get the channel_id
    from skool_list_conversations. Returns sender, content, timestamp per message."""
    return sk.get_conversation(channel_id=channel_id, limit=limit)


@mcp.tool()
def skool_send_message(channel_id: str, content: str) -> dict:
    """Send a DM into an existing conversation. Writes — confirm wording first.
    Get the channel_id from skool_list_conversations."""
    return sk.send_message(channel_id=channel_id, content=content)


@mcp.tool()
def skool_mark_read(channel_id: str) -> dict:
    """Mark a DM conversation as read. Get the channel_id from skool_list_conversations."""
    return sk.mark_read(channel_id=channel_id)


@mcp.tool()
def skool_start_chat(user_id: str) -> dict:
    """Open a new DM with a member (by their user_id from skool_list_members).
    Returns the new conversation so you can then skool_send_message into it."""
    return sk.start_chat(user_id=user_id)


if __name__ == "__main__":
    mcp.run()
