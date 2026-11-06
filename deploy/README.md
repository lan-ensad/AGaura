# Server hardening (Infomaniak VPS)

What protects AGaura on the VPS, outside the application; the installation itself is in
[docs/INSTALL.md](../docs/INSTALL.md), which refers to the sections below. The application keeps
its own limits (`agaura/web/limits.py`, SPEC §9.3): failed logins and wrong session passwords or
codes are refused with `429 Too Many Requests`; an address scanning session IDs gets a neutral
join form that reveals nothing, and still gets in with the right password or code. IPv6
addresses count by /64.

| Layer | Protection | Where |
|---|---|---|
| Network | Volumetric attacks (DDoS) | Infomaniak's network: check what the chosen offer covers |
| Firewall | Ports 22, 80 and 443 only | Infomaniak Manager and the system (`ufw`) |
| SSH | Key only, no root login, fail2ban `sshd` jail | `/etc/ssh/sshd_config`, fail2ban defaults |
| Caddy | Request bodies up to 1 MB (413 beyond), header and body timeouts | `deploy/Caddyfile` |
| fail2ban | Bans robots probing for other software (`/wp-login.php`, `/.env`…) | `deploy/fail2ban/` |
| Application | Limits per address, per session and per account; tribune alert | `agaura/web/limits.py` |

Commands below assume Debian or Ubuntu, the repository in `/srv/agaura` and a sudo user.

## 1. Firewall and SSH

```sh
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw enable
```

Docker publishes its ports through its own rules, which `ufw` does not filter: publish only
Caddy's 80 and 443 (as `compose.yaml` does; the application port stays internal). Apply the
same three ports in the firewall of the Infomaniak Manager when the offer has one.

SSH by key only, without root login. Cloud images include `/etc/ssh/sshd_config.d/*.conf` at the
top of `sshd_config`, and the first value read wins (cloud-init writes `50-cloud-init.conf`): put
both settings in a file read before it, check, reload, and keep the current session open while
a second terminal logs in again:

```sh
printf 'PasswordAuthentication no\nPermitRootLogin no\n' | sudo tee /etc/ssh/sshd_config.d/10-agaura.conf
sudo sshd -t && sudo systemctl reload ssh
sudo sshd -T | grep -E '^(passwordauthentication|permitrootlogin) '   # both "no"
```

Security updates: `sudo apt install unattended-upgrades`.

## 2. Caddy access log for fail2ban

Caddy writes, besides its usual output, a JSON access log with ISO 8601 times to
`/var/log/caddy/access.log` in its container, mounted from `CADDY_LOG_DIR` (`./logs/caddy` by
default). On the VPS, in `/srv/agaura/.env`:

```sh
CADDY_LOG_DIR=/srv/agaura/logs/caddy
```

Secrets in paths and the `Referer` header are redacted there as in the usual output. The file
rolls at 20 MB and old files go after 7 days: client addresses are personal data. The usual
output (`docker compose logs`) holds the same lines with client addresses truncated (/24 in
IPv4, /48 in IPv6, `deploy/Caddyfile`); Docker keeps at most three files of 10 MB per service
(`compose.yaml`), a cap on size rather than age: on a quiet server, lines older than 7 days
remain until newer ones push them out or the containers are recreated
(`docker compose up -d --force-recreate`).

The log must show each visitor's own address. `compose.yaml` enables IPv6 on its internal
network for that: without it, Docker relays IPv6 clients through its userland proxy and every
one of them appears as the network gateway (`172.x.0.1`), sharing a single set of limits.
This needs Docker 27 or later (IPv6 firewall rules on by default). Check from a machine with
IPv6, then read the last line of the log:

```sh
curl -6 -s -o /dev/null https://agaura.org/ipv6-check
tail -n 1 /srv/agaura/logs/caddy/access.log    # "client_ip" must be that machine's IPv6 address
sudo ip6tables -L DOCKER-USER -n                # the chain exists, so fail2ban can ban IPv6 too
```

If `client_ip` shows `172.x.0.1`, upgrade Docker, or remove the `AAAA` DNS record until it is
fixed.

## 3. fail2ban

```sh
sudo apt install fail2ban
sudo rm -f /etc/fail2ban/filter.d/agaura-refused.conf   # jail of earlier versions, now removed
sudo cp deploy/fail2ban/filter.d/agaura-probes.conf /etc/fail2ban/filter.d/
sudo cp deploy/fail2ban/jail.d/agaura.conf /etc/fail2ban/jail.d/
# The filter must find lines and their times in the real log (after a probe such as
# curl -s -o /dev/null https://agaura.org/wp-login.php):
sudo fail2ban-regex /srv/agaura/logs/caddy/access.log /etc/fail2ban/filter.d/agaura-probes.conf
sudo systemctl restart fail2ban
sudo fail2ban-client status agaura-probes
```

| Jail | Bans | Threshold | Ban |
|---|---|---|---|
| `agaura-probes` | `404` on paths AGaura never serves (`.php`, `/wp-`, `/.env`, `/.git`, `/cgi-bin/`…) | 20 in 10 min | 1 h |
| `sshd` (fail2ban default) | failed SSH logins | fail2ban defaults | fail2ban defaults |

Bans go to the `DOCKER-USER` chain: Docker's rules come before `INPUT`, so a ban there would not
stop traffic to Caddy. The `sshd` jail keeps its default action, since SSH is not behind Docker.

Requests the application refuses (`429`) are never banned: a whole room shares one address
(venue Wi-Fi, mobile carriers' CGNAT), and banning it would stop the assembly. The application
limits them itself, finer than an address ban. To lift a ban at once:

```sh
sudo fail2ban-client set agaura-probes unbanip 203.0.113.7
```

fail2ban bans one IPv6 address at a time; the application counts IPv6 by /64, which covers
addresses changed within the same network.

## 4. During an assembly

Someone in the room can request probe paths on purpose and get the venue address banned for
everyone. Before the assembly, look up the public address of the venue network (from a device
on its Wi-Fi, with any "what is my IP" service; both IPv4 and IPv6 if it has both) and exempt it
for the day:

```sh
sudo fail2ban-client set agaura-probes addignoreip 203.0.113.7
sudo fail2ban-client set agaura-probes addignoreip 2001:db8:42:7::/64   # the venue's IPv6 /64
# after the assembly:
sudo fail2ban-client set agaura-probes delignoreip 203.0.113.7
sudo fail2ban-client set agaura-probes delignoreip 2001:db8:42:7::/64
```

Phones on mobile data share their carrier's addresses and cannot be exempted this way.

The status bar of the console warns the tribune from 30 refused passwords or codes in 10
minutes. If the room is not mistyping, change the session password (the QR code and the screen
follow) or lock the entries once everyone has joined. Past 300 refusals in 10 minutes, the
session refuses the addresses that keep failing; voters with the right password still get in.
One address that piles up 60 refused passwords or codes in 10 minutes is refused for new
entries, including the venue address if someone in the room does it on purpose: devices
already in keep voting, and the refusal lifts within 10 minutes once the failures stop.
