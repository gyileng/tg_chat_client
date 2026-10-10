from bisect import bisect_left
from collections import OrderedDict
from html import escape
from math import ceil

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPoint,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QTextDocument, QTextOption
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyledItemDelegate

from .message_cache import HISTORY_LIMIT

SENDER_COLORS = (
    "#a33535",
    "#98521b",
    "#776013",
    "#527020",
    "#247044",
    "#16716b",
    "#236c89",
    "#365dad",
    "#6546a3",
    "#864497",
    "#a23e71",
    "#735547",
)
MESSAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def sender_color(sender_id):
    return "#666666" if sender_id is None else SENDER_COLORS[sender_id % len(SENDER_COLORS)]


def media_text(media):
    names = {"photo": "图片", "sticker": "贴纸", "video": "视频", "audio": "音频", "file": "文件"}
    return "[" + names.get(media, "其他媒体") + "]" if media else ""


class MessageModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.messages = []
        self.ids = []
        self.target = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.messages)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.messages):
            return None
        message = self.messages[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return message.message_id
        if role == MESSAGE_ROLE:
            return message
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return f"{message.sender}：{message.text}\n{media_text(message.media)}\n{message.time}"
        return None

    def index_for(self, message_id):
        row = bisect_left(self.ids, message_id)
        return (
            self.index(row)
            if row < len(self.ids) and self.ids[row] == message_id
            else QModelIndex()
        )

    def sync(self, target, messages):
        ordered = sorted(messages.values(), key=lambda m: m.message_id)
        if target != self.target:
            self.beginResetModel()
            self.target, self.messages = target, ordered
            self.ids = [m.message_id for m in ordered]
            self.endResetModel()
            return
        wanted = set(messages)
        # Contiguous removals avoid one model notification per discarded row.
        row = len(self.ids) - 1
        while row >= 0:
            if self.ids[row] in wanted:
                row -= 1
                continue
            end = row
            while row >= 0 and self.ids[row] not in wanted:
                row -= 1
            self.beginRemoveRows(QModelIndex(), row + 1, end)
            del self.ids[row + 1 : end + 1]
            del self.messages[row + 1 : end + 1]
            self.endRemoveRows()
        row = 0
        while row < len(ordered):
            message = ordered[row]
            if row < len(self.ids) and self.ids[row] == message.message_id:
                if self.messages[row] != message:
                    self.messages[row] = message
                    self.dataChanged.emit(self.index(row), self.index(row))
                row += 1
                continue
            start = row
            while row < len(ordered) and (
                start >= len(self.ids) or ordered[row].message_id < self.ids[start]
            ):
                row += 1
            inserted = ordered[start:row]
            self.beginInsertRows(QModelIndex(), start, row - 1)
            self.messages[start:start] = inserted
            self.ids[start:start] = [m.message_id for m in inserted]
            self.endInsertRows()


class MessageDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.layouts = OrderedDict()

    def layout(self, message, font):
        available = max(24, int((self.view.viewport().width() - 16) * 0.75))
        key = (message, available, font.toString())
        if key in self.layouts:
            self.layouts.move_to_end(key)
            return self.layouts[key]
        name = message.sender
        if message.outgoing and name != "我":
            name += "（我）"
        footer = message.time + (f" · 回复 #{message.reply_to}" if message.reply_to else "")
        content = [f'<p style="color:{sender_color(message.sender_id)}"><b>{escape(name)}</b></p>']
        for text in (message.text, media_text(message.media)):
            if text:
                content.append("<p>" + escape(text).replace("\n", "<br>") + "</p>")
        content.append('<p style="color:#666666">' + escape(footer) + "</p>")
        doc = QTextDocument()
        doc.setDefaultFont(font)
        doc.setDocumentMargin(0)
        option = doc.defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(option)
        doc.setDefaultStyleSheet("p { margin: 0 0 4px 0; color: #202124; white-space: pre-wrap; }")
        doc.setHtml("".join(content))
        doc.setTextWidth(-1)
        width = max(24, min(available, ceil(doc.idealWidth()) + 20))
        doc.setTextWidth(max(1, width - 20))
        size = QSize(width, ceil(doc.size().height()) + 16)
        self.layouts[key] = (doc, size)
        while len(self.layouts) > HISTORY_LIMIT:
            self.layouts.popitem(last=False)
        return doc, size

    def prune(self, messages):
        retained = set(messages)
        for key in list(self.layouts):
            if key[0] not in retained:
                del self.layouts[key]

    def sizeHint(self, option, index):
        _, size = self.layout(index.data(MESSAGE_ROLE), option.font)
        return QSize(self.view.viewport().width(), size.height() + 8)

    def bubble_rect(self, index):
        _, size = self.layout(index.data(MESSAGE_ROLE), self.view.font())
        rect = self.view.visualRect(index)
        x = (
            rect.right() - size.width() - 7
            if index.data(MESSAGE_ROLE).outgoing
            else rect.left() + 8
        )
        return QRectF(x, rect.top() + 4, size.width(), size.height())

    def paint(self, painter, option, index):
        message = index.data(MESSAGE_ROLE)
        doc, _ = self.layout(message, option.font)
        rect = self.bubble_rect(index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e7f3ed" if message.outgoing else "#f1f3f5"))
        painter.drawRoundedRect(rect, 8, 8)
        painter.setClipRect(rect)
        painter.translate(rect.left() + 10, rect.top() + 8)
        doc.drawContents(painter)
        painter.restore()


class MessageList(QListView):
    viewport_resizing = Signal()
    viewport_resized = Signal()

    def __init__(self):
        super().__init__()
        self.setModel(MessageModel(self))
        self.setItemDelegate(MessageDelegate(self))
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Resize:
            self.viewport_resizing.emit()
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Resize:
            self.viewport_resized.emit()
        return result

    def anchor(self):
        if not self.model().rowCount():
            return None
        index = self.indexAt(QPoint(10, 0))
        if not index.isValid():
            index = self.model().index(0)
        return index.data(Qt.ItemDataRole.UserRole), self.visualRect(index).top()

    def restore_anchor(self, anchor):
        if anchor is None:
            self.scrollToBottom()
            return
        index = self.model().index_for(anchor[0])
        if index.isValid():
            self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtTop)
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - anchor[1])
        else:
            self.scrollToTop()
