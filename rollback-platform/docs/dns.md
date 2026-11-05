# DNS

Not automated. Configure on NetBird (match domain `internal`):

| Record             | Points to                       |
|--------------------|---------------------------------|
| `*.dev.internal`   | 100.83.52.191 (prgdlv-test)     |
| `*.prod.internal`  | 100.83.93.156 (prgdlv-auto)     |

Either NetBird's own DNS records, or a small resolver registered as a NetBird nameserver
for the match domain `internal`, for example dnsmasq:

```
address=/dev.internal/100.83.52.191
address=/prod.internal/100.83.93.156
```

## Trusting the CA

Install `docs/ca/dev-root-ca.crt` and `docs/ca/prod-root-ca.crt` into the OS trust store.
On macOS:

```sh
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain docs/ca/dev-root-ca.crt
```

Check: `curl https://app.dev.internal` succeeds without `-k`.
