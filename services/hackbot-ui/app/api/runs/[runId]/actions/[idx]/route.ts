import { NextResponse } from "next/server";

import { HackbotError, updateRunActionText } from "@/lib/hackbot";
import { getAuthedEmail } from "@/lib/session";

export const dynamic = "force-dynamic";

// PATCH /api/runs/:runId/actions/:idx — rewrite one pending action's comment
// body, attributed to the authenticated user.
export async function PATCH(
  req: Request,
  { params }: { params: Promise<{ runId: string; idx: string }> }
) {
  const email = await getAuthedEmail();
  if (!email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { runId, idx } = await params;
  const actionIdx = Number(idx);
  if (!Number.isInteger(actionIdx) || actionIdx < 0) {
    return NextResponse.json(
      { error: "Invalid action index" },
      { status: 400 }
    );
  }

  let text: unknown;
  try {
    ({ text } = await req.json());
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (typeof text !== "string" || !text.trim()) {
    return NextResponse.json(
      { error: "Comment text cannot be empty" },
      { status: 400 }
    );
  }

  try {
    return NextResponse.json(
      await updateRunActionText(runId, actionIdx, text, email)
    );
  } catch (err) {
    const status = err instanceof HackbotError ? err.status : 500;
    return NextResponse.json({ error: (err as Error).message }, { status });
  }
}
