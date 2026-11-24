---- MODULE link_guard_properties ----
EXTENDS link_guard

notLinked == state /= "Linked"
notRefused == state /= "Refused"
notUnchanged == state /= "Unchanged"

neverClobbersWithoutForce ==
    (moved /\ previous = "Inspecting" /\ event \in {"RegularFileNoForce", "DirNoForce"}) =>
        (state = "Refused" /\ outcome = "RefusedOverwrite")

symlinkSameIsNoop ==
    (moved /\ previous = "Inspecting" /\ event = "SymlinkSame") =>
        (state = "Unchanged" /\ outcome = "NoOp")

endsAbsorbing ==
    previous \in {"Unchanged", "Linked", "Refused"} => state = previous
====
