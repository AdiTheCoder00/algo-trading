"use client";

/**
 * Every halt ever requested from this dashboard (or from `algo killswitch`),
 * and whether the engine has acted on it yet.
 *
 * The audit trail matters as much as the button that files a request: three
 * weeks later "who stopped trading on the 14th, and did it actually take" is a
 * question this panel answers without anyone needing to grep a log file. A
 * request with no `acted_on_at` is not a bug — the engine only checks at its
 * next bar (`KillSwitch.tsx`'s own reasoning) — but a request that has sat
 * unacted for a long time is worth being able to see, not just infer.
 */

import { type KillSwitchRequest } from "@/lib/api";
import { shortTime } from "@/lib/api";

interface Props {
  requests: KillSwitchRequest[];
}

export function KillSwitchHistory({ requests }: Props) {
  if (requests.length === 0) {
    return <p className="empty">No halt has ever been requested.</p>;
  }

  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Requested</th>
            <th>By</th>
            <th>Reason</th>
            <th>Flatten</th>
            <th>Acted on</th>
          </tr>
        </thead>
        <tbody>
          {requests.map((request) => (
            <tr key={request.id}>
              <td className="mono">{shortTime(request.requested_at)}</td>
              <td className="mono">{request.requested_by}</td>
              <td className="reason">{request.reason}</td>
              <td className="mono">{request.flatten ? "yes" : "no"}</td>
              <td className={`mono ${request.acted_on_at ? "" : "down"}`}>
                {request.acted_on_at ? shortTime(request.acted_on_at) : "pending"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
