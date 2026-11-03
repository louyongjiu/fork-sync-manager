--------------------------- MODULE SyncPrLifecycle ---------------------------
(***************************************************************************)
(* One fork of scripts/sync.py with dry_run: false, across many workflow   *)
(* runs: the upstream branch, the fork's default branch, the automation    *)
(* branch, and the sync pull request whose head is <owner>:SyncBranch and  *)
(* whose base is TargetBranch.  Each action names the code in              *)
(* scripts/sync.py, or the GitHub rule, that it stands for.                *)
(*                                                                         *)
(* A commit is a record: its id (tip) and the ids of every commit it       *)
(* contains (has).  "a is an ancestor of b" is a.tip \in b.has, which is   *)
(* what git merge-base --is-ancestor answers.                              *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
    TargetBranch,        \* sync_one: target_branch, the fork's default branch
    SyncBranch,          \* settings.sync_branch
    MaxUpstreamCommits,  \* bound: commits the upstream maintainer pushes
    MaxForkCommits,      \* bound: commits you push to the fork's default branch
    MaxRuns              \* bound: workflow runs

VARIABLES
    up,        \* refs/remotes/upstream/<upstream_branch>
    fork,      \* the fork's branches TargetBranch and SyncBranch
    pr,        \* the sync PR, as list_sync_prs() returns it (newest first)
    nextId,    \* id of the next commit
    phase,     \* "idle", or "pr" between a run's git push and its PR calls
    runHead,   \* head_sha that the run passes to ensure_pull_request()
    runUp,     \* the upstream commit that the run fetched
    result,    \* status of the last finished run, as main() logs it
    prCall,    \* what the last run asked GitHub to do with the PR
    pushed,    \* branches that the automation has ever pushed to
    justRan,   \* TRUE in the state right after a run finishes
    nUp, nFork, nRuns

vars == <<up, fork, pr, nextId, phase, runHead, runUp, result, prCall,
          pushed, justRan, nUp, nFork, nRuns>>

Branches == {TargetBranch, SyncBranch}
NoRef == [exists |-> FALSE, tip |-> 0, has |-> {}]
NoPr == [exists |-> FALSE, open |-> FALSE, merged |-> FALSE, head |-> 0]
NoCall == [kind |-> "none", closedHead |-> 0, newHead |-> 0]
Call(kind, closedHead, newHead) ==
    [kind |-> kind, closedHead |-> closedHead, newHead |-> newHead]

Commit(parents) ==
    [exists |-> TRUE, tip |-> nextId, has |-> {nextId} \cup UNION {p.has : p \in parents}]

\* is_ancestor(cwd, older, newer)
IsAncestor(a, b) == b.exists /\ a.tip \in b.has

SyncTip == fork[SyncBranch].tip

TypeOK ==
    /\ phase \in {"idle", "pr"}
    /\ result \in {"none", "up_to_date", "updated", "conflict", "skipped", "failed"}
    /\ prCall.kind \in {"none", "create", "update", "reopen", "reopen_refused", "create_refused"}
    /\ pushed \subseteq Branches
    /\ pr.exists \/ pr = NoPr
    /\ pr.merged => ~pr.open

Init ==
    /\ up = [exists |-> TRUE, tip |-> 0, has |-> {0}]
    /\ fork = [b \in Branches |-> IF b = TargetBranch THEN up ELSE NoRef]
    /\ pr = NoPr
    /\ nextId = 1
    /\ phase = "idle"
    /\ runHead = 0
    /\ runUp = 0
    /\ result = "none"
    /\ prCall = NoCall
    /\ pushed = {}
    /\ justRan = FALSE
    /\ nUp = 0
    /\ nFork = 0
    /\ nRuns = 0

----------------------------------------------------------------------------
(* People and the upstream                                                 *)

UpstreamAdvances ==
    /\ nUp < MaxUpstreamCommits
    /\ up' = Commit({up})
    /\ nextId' = nextId + 1
    /\ nUp' = nUp + 1
    /\ justRan' = FALSE
    /\ UNCHANGED <<fork, pr, phase, runHead, runUp, result, prCall, pushed, nFork, nRuns>>

YouCommitToDefaultBranch ==
    /\ nFork < MaxForkCommits
    /\ fork' = [fork EXCEPT ![TargetBranch] = Commit({fork[TargetBranch]})]
    /\ nextId' = nextId + 1
    /\ nFork' = nFork + 1
    /\ justRan' = FALSE
    /\ UNCHANGED <<up, pr, phase, runHead, runUp, result, prCall, pushed, nUp, nRuns>>

\* GitHub "Create a merge commit" adds a commit with both parents; "Squash and
\* merge" adds one commit on the base only.  deleteBranch is the repository's
\* "Automatically delete head branches" setting.  GitHub freezes head.sha.
YouMergePr(method, deleteBranch) ==
    /\ pr.exists /\ pr.open
    /\ LET t == fork[TargetBranch]
           s == fork[SyncBranch]
           m == IF method = "merge" THEN Commit({t, s}) ELSE Commit({t})
       IN fork' = [b \in Branches |->
                     IF b = TargetBranch THEN m
                     ELSE IF deleteBranch THEN NoRef ELSE fork[b]]
    /\ pr' = [pr EXCEPT !.open = FALSE, !.merged = TRUE, !.head = SyncTip]
    /\ nextId' = nextId + 1
    /\ justRan' = FALSE
    /\ UNCHANGED <<up, phase, runHead, runUp, result, prCall, pushed, nUp, nFork, nRuns>>

\* GitHub freezes head.sha when a PR closes.
YouClosePr ==
    /\ pr.exists /\ pr.open
    /\ pr' = [pr EXCEPT !.open = FALSE, !.head = SyncTip]
    /\ justRan' = FALSE
    /\ UNCHANGED <<up, fork, nextId, phase, runHead, runUp, result, prCall, pushed, nUp, nFork, nRuns>>

----------------------------------------------------------------------------
(* One run of sync_one() for this fork                                     *)

\* sync_one lines 340-345: reusable
Reusable(t, s) == s.exists /\ IsAncestor(t, s) /\ IsAncestor(up, s)

\* sync_one lines 291-295: raise SyncError before any git command
SyncBranchIsTarget == SyncBranch = TargetBranch

\* sync_one lines 291-386: the sync_branch check, then fetch both branches,
\* then up_to_date (line 322), reuse (lines 347-349), conflict (lines
\* 357-365), or rebuild the automation branch from the target, merge
\* upstream and push it (lines 351-386).  A conflict can happen on any
\* merge: the model does not track file contents.
RunFetchesAndPushes ==
    /\ phase = "idle"
    /\ nRuns < MaxRuns
    /\ nRuns' = nRuns + 1
    /\ prCall' = NoCall
    /\ UNCHANGED <<up, pr, nUp, nFork>>
    /\ LET t == fork[TargetBranch]
           s == fork[SyncBranch]
       IN IF SyncBranchIsTarget
          THEN /\ result' = "failed"
               /\ justRan' = TRUE
               /\ UNCHANGED <<fork, nextId, phase, runHead, runUp, pushed>>
          ELSE IF IsAncestor(up, t)
          THEN /\ result' = "up_to_date"
               /\ justRan' = TRUE
               /\ UNCHANGED <<fork, nextId, phase, runHead, runUp, pushed>>
          ELSE IF Reusable(t, s)
          THEN /\ phase' = "pr"
               /\ runHead' = s.tip
               /\ runUp' = up.tip
               /\ justRan' = FALSE
               /\ UNCHANGED <<fork, nextId, result, pushed>>
          ELSE \/ /\ result' = "conflict"
                  /\ justRan' = TRUE
                  /\ UNCHANGED <<fork, nextId, phase, runHead, runUp, pushed>>
               \/ /\ fork' = [fork EXCEPT ![SyncBranch] = Commit({t, up})]
                  /\ nextId' = nextId + 1
                  /\ pushed' = pushed \cup {SyncBranch}
                  /\ phase' = "pr"
                  /\ runHead' = nextId
                  /\ runUp' = up.tip
                  /\ justRan' = FALSE
                  /\ UNCHANGED result

\* GitHub: a merged PR never reopens, and a closed PR reopens only while its
\* branch still holds the head it had when it closed (no force-push, no
\* delete and recreate).
CanReopen ==
    /\ ~pr.merged
    /\ fork[SyncBranch].exists
    /\ pr.head \in fork[SyncBranch].has

\* GitHub refuses POST /pulls when the head adds nothing to the base.
NoCommitsBetween(h) == h \in fork[TargetBranch].has

\* ensure_pull_request line 196: old_sha == head_sha
ClosedPrUnchanged == pr.head = runHead

Finish(status, call) ==
    /\ result' = status
    /\ prCall' = call
    /\ phase' = "idle"
    /\ justRan' = TRUE

\* ensure_pull_request lines 213-224: POST /pulls.  The new PR is the newest
\* one, so list_sync_prs() returns it first from now on.
CreatePr ==
    IF NoCommitsBetween(runHead)
    THEN /\ Finish("failed", Call("create_refused", 0, runHead))
         /\ UNCHANGED pr
    ELSE /\ pr' = [exists |-> TRUE, open |-> TRUE, merged |-> FALSE, head |-> runHead]
         /\ Finish("updated", Call("create", 0, runHead))

\* ensure_pull_request lines 201-211: no PATCH for a merged PR, and a 422 from
\* PATCH state=open falls through to POST /pulls.
WhenReopenImpossible == CreatePr

\* ensure_pull_request lines 170-224 and the status mapping at lines 421-425.
\* Any other HTTP error from GitHub raises SyncError, and main() logs FAILED.
\* The PATCH state=open at lines 203-208 succeeds exactly when CanReopen.
EnsurePr ==
    IF ~pr.exists
    THEN CreatePr
    ELSE IF pr.open
    THEN /\ Finish("updated", Call("update", 0, runHead))
         /\ UNCHANGED pr
    ELSE IF ClosedPrUnchanged
    THEN /\ Finish("skipped", NoCall)
         /\ UNCHANGED pr
    ELSE IF CanReopen
    THEN /\ pr' = [pr EXCEPT !.open = TRUE]
         /\ Finish("updated", Call("reopen", pr.head, runHead))
    ELSE WhenReopenImpossible

\* sync_one lines 391-425
RunUpdatesPr ==
    /\ phase = "pr"
    /\ EnsurePr
    /\ UNCHANGED <<up, fork, nextId, runHead, runUp, pushed, nUp, nFork, nRuns>>

\* End of the bounded scenario.
Done ==
    /\ phase = "idle"
    /\ nRuns = MaxRuns
    /\ UNCHANGED vars

Next ==
    \/ UpstreamAdvances
    \/ YouCommitToDefaultBranch
    \/ \E method \in {"merge", "squash"}, deleteBranch \in BOOLEAN :
          YouMergePr(method, deleteBranch)
    \/ YouClosePr
    \/ RunFetchesAndPushes
    \/ RunUpdatesPr
    \/ Done

Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
(* Promises                                                                *)

\* While GitHub and the network work, a run never fails because of the PRs
\* that earlier runs opened.
NoFailedRun == result # "failed"

\* The automation never pushes the fork's default branch.
DefaultBranchNeverPushed == TargetBranch \notin pushed

\* A PR that you closed is not reopened while the automation branch still
\* holds the commit it had when you closed it.
ClosedPrNotReopenedUnchanged ==
    prCall.kind = "reopen" => prCall.closedHead # prCall.newHead

\* A run that reports "updated" leaves an open sync PR that contains the
\* upstream commit that the run fetched.
UpdatedRunLeavesOpenPrWithUpstream ==
    (justRan /\ result = "updated") =>
        /\ pr.exists
        /\ pr.open
        /\ runUp \in fork[SyncBranch].has
=============================================================================
