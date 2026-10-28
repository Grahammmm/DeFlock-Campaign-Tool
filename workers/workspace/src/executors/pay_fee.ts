// pay_fee: records fees are paid by a person, never by this system. The executor refuses
// with instructions; the approved card is the organizer's authorization record.
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

export const PAY_FEE_INSTRUCTIONS =
  "Pay the fee manually: confirm the agency's fee estimate against the request's fee cap, pay through the agency's " +
  "official channel, keep the receipt as an original (ingest it by hash), then attach the payment reference to this " +
  "card with Edit. No card, bank or payment credential is stored or used by the workspace.";

export const payFeeExecutor: ActionExecutor = {
  kind: "pay_fee",
  async execute(_action, proposal) {
    const cap = typeof proposal.fee_cap_cents === "number" ? proposal.fee_cap_cents : null;
    const amount = typeof proposal.amount_cents === "number" ? proposal.amount_cents : null;
    const over = cap !== null && amount !== null && amount > cap ? ` The proposed amount (${amount} cents) exceeds the request fee cap (${cap} cents).` : "";
    throw new ExecutorFailure("manual_only", PAY_FEE_INSTRUCTIONS + over);
  },
};
