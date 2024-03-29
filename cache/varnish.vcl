vcl 4.0;

backend default {
 .host = "127.127.127.127";
 .port = "12721";
}

/* from https://github.com/varnishcache/varnish-cache/blob/master/bin/varnishd/builtin.vcl with some modifications */
sub vcl_recv {
    if (req.method == "PRI") {
        /* This will never happen in properly formed traffic (see: RFC7540) */
        return (synth(405));
    }
    if (!req.http.host &&
      req.esi_level == 0 &&
      req.proto ~ "^(?i)HTTP/1.1") {
        /* In HTTP/1.1, Host is required. */
        return (synth(400));
    }
    if (req.method != "GET" &&
      req.method != "HEAD" &&
      req.method != "PUT" &&
      req.method != "POST" &&
      req.method != "TRACE" &&
      req.method != "OPTIONS" &&
      req.method != "DELETE" &&
      req.method != "PATCH") {
        /* Non-RFC2616 or CONNECT which is weird. */
        return (pipe);
    }

    if (req.method != "GET" && req.method != "HEAD") {
        /* We only deal with GET and HEAD by default */
        return (pass);
    }
    if (req.url ~ "^/verify") {
        return (pass);
    }
    return (hash);
}

sub vcl_hash {
    hash_data(req.url);
    hash_data(req.http.X-Character);
    return (lookup);
}

sub vcl_backend_response {
    if (bereq.uncacheable) {
        return (deliver);
    }
    set beresp.keep = 1d;
    return (deliver);
}
