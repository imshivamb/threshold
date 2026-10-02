// Skeleton status page: proves api, Ring adapter and ingestion are wired up.
// The real Today / Timeline / Case screens come later.
export const dynamic = "force-dynamic";

const API_URL = process.env.API_URL ?? "http://localhost:8000";

type Health = { ring_mode: string; ring_url: string; token: { has_token: boolean; expired: boolean } };
type Device = { id: string; name: string; kind: string; online: boolean | null };
type RingEvent = { id: number; type: string; sub_type: string | null; started_at: string; source: string; device_id: string | null };

async function get<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

export default async function Home() {
  const [health, devices, events] = await Promise.all([
    get<Health>("/health"),
    get<Device[]>("/devices"),
    get<RingEvent[]>("/events?limit=25"),
  ]);
  const names = new Map((devices ?? []).map((d) => [d.id, d.name]));

  return (
    <main>
      <h1>Threshold</h1>
      <p className="muted">
        {health
          ? `API up · Ring mode: ${health.ring_mode} · token: ${
              health.token.expired ? "expired — paste a new one" : health.token.has_token ? "set" : "missing"
            }`
          : `API unreachable at ${API_URL}`}
      </p>

      <h2>Devices</h2>
      {devices?.length ? (
        <table>
          <tbody>
            {devices.map((d) => (
              <tr key={d.id}>
                <td>{d.name}</td>
                <td className="muted">{d.kind}</td>
                <td>{d.online === null ? "unknown" : d.online ? "online" : "offline"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No devices yet.</p>
      )}

      <h2>Latest events</h2>
      {events?.length ? (
        <table>
          <thead>
            <tr><th>When (UTC)</th><th>Device</th><th>Type</th><th>Source</th></tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td>{new Date(e.started_at).toISOString().replace("T", " ").slice(0, 19)}</td>
                <td>{(e.device_id && names.get(e.device_id)) ?? e.device_id ?? "—"}</td>
                <td>{e.type}{e.sub_type ? ` · ${e.sub_type}` : ""}</td>
                <td className="muted">{e.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No events yet. Try <code>ring-sandbox play delivery --speed 5</code>.</p>
      )}
    </main>
  );
}
