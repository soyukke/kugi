---- MODULE store_publish_properties ----
EXTENDS store_publish

notPublished == state /= "Published"
notAborted == state /= "Aborted"

stagedOnlyAfterHashVerified ==
    state = "Staged" =>
        (previous = "Staged" \/ (previous = "HashVerified" /\ event = "StageOk" /\ outcome = "Applied"))

publishedOnlyAfterVerifyOrHit ==
    state = "Published" =>
        (previous = "Published"
            \/ (previous = "Locked" /\ event = "EntryPresent" /\ outcome = "Applied")
            \/ (previous = "Staged" /\ event \in {"RenameOk", "RenameLostRace"} /\ outcome = "Applied"))

hashMismatchAborts ==
    (moved /\ previous = "PartReady" /\ event = "HashMismatch") => state = "Aborted"

endsAbsorbing ==
    previous \in {"Published", "Aborted"} => state = previous
====
