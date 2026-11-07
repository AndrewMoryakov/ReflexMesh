"""`reflexmesh browser serve` where a continued attempt of a new revision starts with the first navigation to start_url.

An experiment, not a feature: it patches one method from outside. The navigation is the regular one, through the same
gate, step budget, slot ledger and journal; the session, the profile and the claim stay the chain's.
"""
import sys
from reflexmesh.cli import main
from reflexmesh.runtime import browser_run as br

reset = br.BrowserControlledEnvironment.reset


def reset_on_new_revision(self, goal):
    if self.continued and self.task.revision > 1:
        before = (self.inner.session or {}).get("pid")
        self.continued = False
        try:
            return reset(self, goal)
        finally:
            self.continued = True
            after = (self.inner.session or {}).get("pid")
            self.journal.emit("experiment_restart", browser_pid_before=before, browser_pid_after=after, same=before == after)
    return reset(self, goal)


br.BrowserControlledEnvironment.reset = reset_on_new_revision
sys.exit(main(sys.argv[1:]))
