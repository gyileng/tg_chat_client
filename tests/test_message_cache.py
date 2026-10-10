from tg_chat_client.message_cache import CHAT_LIMIT, HISTORY_LIMIT, RECENT_LIMIT, MessageCache
from tg_chat_client.models import ChatMessage


def message(aid, mid, chat=-1):
    return ChatMessage(aid, chat, mid, "群友", f"消息 {mid}", "12:00")


def test_five_accounts_10000_messages_each_keep_only_recent_window():
    cache = MessageCache()
    for aid in map(str, range(5)):
        cache.open((aid, -1))
        for mid in range(10000):
            cache.put(message(aid, mid))
        assert len(cache.recent[aid, -1]) == RECENT_LIMIT
        assert min(cache.recent[aid, -1]) == 10000 - RECENT_LIMIT
    assert sum(map(len, cache.recent.values())) == RECENT_LIMIT * 5


def test_history_browse_survives_live_traffic_and_loads_beyond_recent_cache():
    cache = MessageCache()
    target = ("a", -1)
    cache.open(target)
    for mid in range(1000, 1500):
        cache.put(message("a", mid))
    cache.browse(target)
    for mid in range(1500, 11500):
        cache.put(message("a", mid))
    assert min(cache.view(target)) == 1000
    assert max(cache.view(target)) == 1499
    cache.older(target, [message("a", mid) for mid in range(950, 1000)])
    assert len(cache.view(target)) == HISTORY_LIMIT
    assert min(cache.view(target)) == 950
    assert max(cache.view(target)) == 1449
    cache.bottom(target)
    assert min(cache.view(target)) == 11000
    assert max(cache.view(target)) == 11499


def test_visits_control_lru_and_uncached_live_groups_are_ignored():
    cache = MessageCache()
    targets = [("a", chat) for chat in range(CHAT_LIMIT)]
    for target in targets:
        cache.open(target)
        cache.put(message("a", 1, target[1]))
    cache.browse(targets[1])
    cache.open(targets[0])
    cache.put(message("a", 2, targets[1][1]))
    assert cache.open(("a", 100)) == targets[1]
    assert targets[1] not in cache.history
    for chat in range(1000, 11000):
        assert not cache.put(message("a", 1, chat))
    assert len(cache.recent) == CHAT_LIMIT
    assert targets[0] in cache.recent


def test_duplicates_update_in_place_in_both_windows():
    cache = MessageCache()
    target = ("a", -1)
    cache.open(target)
    cache.put(message("a", 1))
    cache.browse(target)
    changed = ChatMessage("a", -1, 1, "群友", "更正", "12:00")
    cache.put(changed)
    assert list(cache.recent[target].values()) == [changed]
    assert list(cache.view(target).values()) == [changed]
