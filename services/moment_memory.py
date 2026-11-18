"""Build short-memory records directly from published Moments content."""


def format_moment_memory(post_content, *, author_name=None, comment_content=None,
                         reply_to_name=None, upstream_content=None):
    if comment_content is None:
        return f"[朋友圈] 我发布的内容：\n{post_content}"
    context = f"[朋友圈] {author_name}的朋友圈：\n{post_content}"
    if reply_to_name is not None:
        upstream = upstream_content if upstream_content is not None else "（原评论已不可用）"
        return f"{context}\n\n上游评论（{reply_to_name}）：\n{upstream}\n\n我的回复：\n{comment_content}"
    return f"{context}\n\n我的评论：\n{comment_content}"


def moment_comment_snapshot(comment):
    """Keep the selected parent without recursively copying the reply tree."""
    return {key: comment.get(key, "") for key in ("commenter_id", "content", "timestamp")}


def moment_reply_source(comments, comment_index):
    """Use the saved parent; old records only identify the person replied to."""
    comment = comments[comment_index]
    reply_to = comment.get("reply_to")
    if not reply_to:
        return None
    source = comment.get("reply_to_comment")
    if isinstance(source, dict) and source.get("commenter_id") == reply_to:
        return source
    for previous in reversed(comments[:comment_index]):
        if previous.get("commenter_id") == reply_to:
            return previous
    return None
