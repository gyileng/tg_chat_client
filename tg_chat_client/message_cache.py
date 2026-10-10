from collections import OrderedDict

RECENT_LIMIT = 500
HISTORY_LIMIT = 500
CHAT_LIMIT = 10


class MessageCache:
    """Only visited chats are cached; incoming traffic never changes the visit order."""

    def __init__(self):
        self.recent = OrderedDict()
        self.history = {}

    def open(self, target):
        self.recent.setdefault(target, {})
        self.recent.move_to_end(target)
        evicted = None
        if len(self.recent) > CHAT_LIMIT:
            evicted, _ = self.recent.popitem(last=False)
            self.history.pop(evicted, None)
        return evicted

    def put(self, message):
        target = message.account_id, message.chat_id
        if target not in self.recent:
            return False
        recent = self.recent[target]
        recent[message.message_id] = message
        if len(recent) > RECENT_LIMIT:
            del recent[min(recent)]
        # Update duplicates already visible in history, without appending new traffic.
        if message.message_id in self.history.get(target, {}):
            self.history[target][message.message_id] = message
        return True

    def browse(self, target):
        if target in self.recent:
            self.history.setdefault(target, dict(self.recent[target]))

    def older(self, target, messages):
        if target not in self.recent:
            return
        self.browse(target)
        window = self.history[target]
        window.update((m.message_id, m) for m in messages)
        for mid in sorted(window)[HISTORY_LIMIT:]:
            del window[mid]

    def view(self, target):
        return self.history.get(target, self.recent.get(target, {}))

    def bottom(self, target):
        self.history.pop(target, None)

    def remove(self, target):
        self.recent.pop(target, None)
        self.history.pop(target, None)
