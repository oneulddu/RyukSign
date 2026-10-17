# HTTP downloads on private networks

RyukSign accepts user-supplied HTTP IPA URLs on RFC1918 IPv4 networks
(`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and the Tailscale IPv4
range (`100.64.0.0/10`). The app plist explicitly grants these ranges
`NSExceptionAllowsInsecureHTTPLoads`; public HTTP addresses remain subject to ATS.

On newer systems, the presence of `NSAllowsLocalNetworking` makes ATS ignore
`NSAllowsArbitraryLoads`. Starting in iOS 17/macOS 14, IP addresses and CIDR
ranges can be listed in `NSExceptionDomains`. Both keys alone therefore do not
suffice for a remote Tailscale IPv4 HTTP download. The legacy keys remain for
existing local-host behavior. No TLS version, forward-secrecy, or certificate-trust
relaxation is added. Matching exceptions use the default ATS HTTPS requirements.

Sources: [Apple local networking policy](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking)
and [IP/CIDR exceptions](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsexceptiondomains).

The macOS Foundation check uses non-self addresses because a machine's own IP
can already pass the local-network policy. The old plist produces ATS error -1022
for a remote Tailscale IP; the new policy reaches the connection attempt instead.
A connection refusal or timeout proves only that ATS did not block that attempt.
The iPhone transfer and iOS 16 behavior still require device validation.

These IPv4 exceptions do not change DNS resolution, grant access to `.ts.net`
hostnames, or cover IPv6 URLs. They also do not alter the signing UI's optional
Info.plist presets for other apps.
