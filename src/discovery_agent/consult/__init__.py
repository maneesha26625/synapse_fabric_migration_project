"""Consultation: the questions a migration is decided by, and the brief a person approves.

The catalog (``catalog.json``) holds the questions as data. Facts are computed once per discovery snapshot; the
engine decides which questions apply, what each one recommends and why, which answers contradict the estate, and
what to ask next. Answers are append-only. An approved brief is an immutable, hashed version of every decision,
and one policy object (``policy``) is built from it: the only thing the rest of the accelerator reads. Code reads
named settings (``data.mode``, ``security.sqlUserMap``), never question ids.

Pure: no I/O but reading the catalog file. The project's record and the HTTP surface are in ``api.consult``.
"""
