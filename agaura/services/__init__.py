"""Business rules on top of the models.

Admin functions take the current ``owner`` and only ever reach that account's data (FR-07).
Functions that write expect to run inside ``Database.write()``, which commits or rolls back the
whole transaction; they flush but never commit themselves.
"""
