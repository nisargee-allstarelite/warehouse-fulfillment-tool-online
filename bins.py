"""
Bin pool - hands out one of a FIXED number of numbered bins (physical
shelf/cart slots in the warehouse) to each order currently in the active
batch, so picking can happen product-by-product (one walk per SKU) while
items still land pre-sorted into the right order's bin, instead of
someone having to sort a pile back into orders afterward.

Per Nisargee: 30 bins at a time, not an ever-growing count. Which order
gets a bin - and which order gets the NEXT bin once one frees up - is
decided by batching.py (age + ship-deadline + product-overlap scoring).
This class only tracks the low-level bookkeeping: which fo_id currently
holds which bin number, and which numbers are free right now.

Bins are never taken away from an order once assigned (see batching.py's
docstring for why) - the only way a bin frees up is release(), called
when that order actually ships.
"""


class BinPool:
    def __init__(self, assigned=None, free=None, next_new=1, max_bins=30):
        # assigned: {fulfillment_order_id: bin_number}
        self.assigned = dict(assigned or {})
        # free: bin numbers that were in use and got released (order shipped,
        # or was manually cleared) - handed out again before minting new ones.
        self.free = list(free or [])
        self.next_new = next_new
        self.max_bins = max_bins

    def to_dict(self):
        return {"assigned": self.assigned, "free": self.free, "next_new": self.next_new}

    @classmethod
    def from_dict(cls, d, max_bins=30):
        if not d:
            return cls(max_bins=max_bins)
        return cls(
            assigned=d.get("assigned", {}), free=d.get("free", []),
            next_new=d.get("next_new", 1), max_bins=max_bins,
        )

    def bin_for(self, fo_id):
        return self.assigned.get(fo_id)

    def has_capacity(self):
        return len(self.assigned) < self.max_bins

    def assign(self, fo_id):
        """Give fo_id the lowest available bin number, if it doesn't
        already have one. Reuses a freed number before minting a new
        one. Raises if every bin (1..max_bins) is already in use - the
        caller (server.py) should only call this when has_capacity() is
        true / there's a slot batching.py actually chose to fill, so
        this is a safety net, not the normal path."""
        if fo_id in self.assigned:
            return self.assigned[fo_id]
        if not self.has_capacity():
            raise RuntimeError(f"All {self.max_bins} bins are already in use - can't assign a new one.")
        if self.free:
            self.free.sort()
            bin_number = self.free.pop(0)
        else:
            bin_number = self.next_new
            self.next_new += 1
        self.assigned[fo_id] = bin_number
        return bin_number

    def release(self, fo_id):
        """Free up fo_id's bin (order shipped / removed) so it can be
        reused by the next order batching.py picks."""
        bin_number = self.assigned.pop(fo_id, None)
        if bin_number is not None and bin_number not in self.free:
            self.free.append(bin_number)
        return bin_number

    def reassign(self, fo_id, new_bin_number):
        """Manual override - move an order to a specific bin number
        (e.g. two orders got mixed up on the shelf). Must already hold a
        bin (only active/batched orders have one). Whatever bin fo_id
        previously held becomes free. Rejected if new_bin_number is
        outside 1..max_bins, or already held by another order."""
        if fo_id not in self.assigned:
            raise ValueError("That order isn't in the active batch yet, so it has no bin to move.")
        if not (1 <= new_bin_number <= self.max_bins):
            raise ValueError(f"Bin numbers must be between 1 and {self.max_bins}.")
        for other_fo, other_bin in self.assigned.items():
            if other_fo != fo_id and other_bin == new_bin_number:
                raise ValueError(f"Bin {new_bin_number} is already in use by another order.")

        old_bin = self.assigned.get(fo_id)
        self.assigned[fo_id] = new_bin_number
        if old_bin is not None and old_bin != new_bin_number and old_bin not in self.free:
            self.free.append(old_bin)
        if new_bin_number in self.free:
            self.free.remove(new_bin_number)
        if new_bin_number >= self.next_new:
            self.next_new = new_bin_number + 1
        return new_bin_number

    def in_use_count(self):
        return len(self.assigned)
