// post_social: no social provider adapter exists. Approving the card records the decision;
// execution fails with a stable code so the organizer posts by hand and records the URL.
import { ExecutorFailure, type ActionExecutor } from "./types.ts";

export const postSocialExecutor: ActionExecutor = {
  kind: "post_social",
  async execute() {
    throw new ExecutorFailure(
      "not_implemented",
      "no social posting adapter is registered; post the approved text manually and record the post URL on the card via Edit",
    );
  },
};
