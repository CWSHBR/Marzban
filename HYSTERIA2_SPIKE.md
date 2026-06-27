# Hysteria2 spike result

- Xray version pin: `Dockerfile` defaults to `ARG XRAY_VERSION=v26.3.27` and passes it to `https://github.com/Gozargah/Marzban-scripts/raw/master/install_latest_xray.sh`.
- Xray runtime target: `v26.3.27`.
- Hysteria protocol name in Xray config: `hysteria`.
- Hysteria2 version field: `version = 2` in inbound/outbound `settings` and `streamSettings.hysteriaSettings`.
- Runtime users array: `settings.users`, not `settings.clients`.
- Marzban Hysteria2 policy: require `streamSettings.network = "hysteria"` and `streamSettings.security = "tls"`; Reality and non-TLS Hysteria2 are rejected for consistent subscription/client outputs.
- Dynamic gRPC support: no in the current generated `xray_api/proto`; no local `proxy/hysteria` protobuf files were found.
- Fallback strategy: use full Xray config rebuild/restart for user operations that touch Hysteria2; keep existing gRPC `AlterInbound` flow for VMess, VLESS, Trojan, and Shadowsocks.
- Docker files found: `Dockerfile`, `docker-compose.yml`, `.dockerignore`.
- Frontend protocol hardcodes found in `app/dashboard/src/types/User.ts`, `app/dashboard/src/contexts/DashboardContext.tsx`, `app/dashboard/src/components/UserDialog.tsx`, and `app/dashboard/src/components/RadioGroup.tsx`.
