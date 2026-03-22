from blinker import Namespace


pizza_delivery_signals = Namespace()

entry_created = pizza_delivery_signals.signal('entry-created')
entry_deleted = pizza_delivery_signals.signal('entry-deleted')
entry_delivered = pizza_delivery_signals.signal('entry-delivered')
entry_updated = pizza_delivery_signals.signal('entry-updated')
entry_claimed = pizza_delivery_signals.signal('entry-claimed')
