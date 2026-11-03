----------------------- MODULE SyncPrLifecycleVariants -----------------------
(***************************************************************************)
(* Operators that a .cfg file swaps into SyncPrLifecycle with "<-".  Each  *)
(* one removes one check from scripts/sync.py, to show that the promise    *)
(* that check guards can fail.                                             *)
(***************************************************************************)
EXTENDS SyncPrLifecycle

\* Break for ClosedPrNotReopenedUnchanged: drop the old_sha == head_sha check
\* at ensure_pull_request line 196.
ClosedPrUnchangedDropped == FALSE

\* Break for UpdatedRunLeavesOpenPrWithUpstream: reuse the automation branch
\* without is_ancestor(upstream_ref, sync_ref) at sync_one line 344.
ReusableWithoutUpstreamCheck(t, s) == s.exists /\ IsAncestor(t, s)

\* Break for NoFailedRun: drop the merged_at check and the 422 fallback at
\* ensure_pull_request lines 201-211, so every changed closed PR gets
\* PATCH state=open and a refusal raises SyncError.
WhenReopenImpossibleFails ==
    /\ Finish("failed", Call("reopen_refused", pr.head, runHead))
    /\ UNCHANGED pr

\* Break for DefaultBranchNeverPushed: drop the sync_branch check at sync_one
\* lines 291-295.
SyncBranchCheckDropped == FALSE
=============================================================================
