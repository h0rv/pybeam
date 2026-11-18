# Can one crashed request disconnect other users?

Rooms group messages, not server processes. Users in different rooms can share
the same web worker. If native code crashes that worker, all its connections
close, including connections from other rooms.

This example compares that shared worker with PyBEAM running the dangerous task
in a separate process. It does **not** mean every Python exception disconnects
users, or that PyBEAM automatically isolates every task.
