---- MODULE fetch_transport_properties ----
EXTENDS fetch_transport

notDone == state /= "Done"
notCurlHttps == state /= "CurlHttps"
notFailed == state /= "Failed"

noHttpsDowngrade ==
    (moved /\ previous = "NativeHttps" /\ event = "RedirectHttp") =>
        (state = "Failed" /\ outcome = "Rejected")

curlOnlyFromHttps ==
    state = "CurlHttps" =>
        (previous = "CurlHttps" \/ (previous = "NativeHttps" /\ event = "TlsOrNetError" /\ outcome = "Applied"))

doneOnlyFrom200OrCurlOk ==
    state = "Done" =>
        (previous = "Done"
            \/ (previous \in {"NativeHttp", "NativeHttps"} /\ event = "Http200" /\ outcome = "Applied")
            \/ (previous = "CurlHttps" /\ event = "CurlOk" /\ outcome = "Applied"))

endsAbsorbing ==
    previous \in {"Done", "Failed"} => state = previous
====
