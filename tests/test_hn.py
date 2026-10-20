from forumread.sources import hn


def test_item_tree(fake_http, fixtures):
    fake_http["/items/8863"] = fixtures("hn_item.json")
    thread = hn.fetch_thread("8863")
    assert thread.source == "hn" and thread.url == "https://news.ycombinator.com/item?id=8863"
    assert thread.title.startswith("My YC app: Dropbox")
    assert thread.body == "Link: http://www.getdropbox.com/u/2/screencast.html"
    assert thread.score == 104
    assert len(thread.comments) == 6
    first = thread.comments[0]
    assert (first.author, first.body, first.depth) == ("dhouston", "oh, and a mac port is coming :)", 0)
    assert first.children[0].depth == 1
    assert thread.comments[1].children[0].children[0].depth == 2
    assert "<p>" not in thread.comments[1].body and "\n" in thread.comments[1].body


def test_top_level_comments_follow_the_site_ranking_and_unranked_ones_go_last(fake_http):
    def comment(cid, author):
        return {"id": cid, "type": "comment", "author": author, "text": "x", "children": []}

    fake_http["/items/9"] = {"id": 9, "type": "story", "title": "T", "children": [
        comment(1, "oldest"), comment(2, "middle"), comment(3, "newest"), comment(4, "unranked")]}
    fake_http["hacker-news.firebaseio.com"] = {"id": 9, "kids": [3, 1, 2]}
    assert [c.author for c in hn.fetch_thread("9").comments] == ["newest", "oldest", "middle", "unranked"]


def test_ranking_outage_keeps_the_algolia_order(fake_http, fixtures):
    from forumread.http import FetchError

    fake_http["/items/8863"] = fixtures("hn_item.json")
    fake_http["hacker-news.firebaseio.com"] = FetchError("hacker-news.firebaseio.com: HTTP 503")
    assert hn.fetch_thread("8863").comments[0].author == "dhouston"


def test_comment_link_resolves_to_story(fake_http, fixtures):
    story = fixtures("hn_item.json")
    fake_http["/items/8865"] = {"id": 8865, "type": "comment", "story_id": 8863, "children": []}
    fake_http["/items/8863"] = story
    assert hn.fetch_thread("8865").id == "8863"


def test_search(fake_http, fixtures):
    fake_http["/search"] = fixtures("hn_search.json")
    hits = hn.search("rust", 2).hits
    assert hits[0].url.startswith("https://news.ycombinator.com/item?id=")
    assert hits[0].num_comments is not None
