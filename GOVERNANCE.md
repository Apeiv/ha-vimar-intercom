# Governance

How this project is run: who does what, who tests on which intercom, and how changes get in.
Small on purpose; it changes with a PR like everything else.

## Roles

| Person | GitHub role | Areas |
|---|---|---|
| Lollo ([@lollox80](https://github.com/lollox80)) | Admin, maintainer | SIP, door and actuators, phonebook, releases, repository settings |
| Apeiv ([@Apeiv](https://github.com/Apeiv)) | Triage | card, `/av` and media, cloud relay behaviour |
| m4r1k ([@m4r1k](https://github.com/m4r1k)) | Triage | HomeKit, SRTP |

- **Triage**: label, assign and close issues and PRs, review. Code still comes in through PRs from a fork.
- **Write** for Apeiv and m4r1k once the required checks are on `main` (CI and "PR text" must be green before
  any merge). With Write they become code owners of their areas (`.github/CODEOWNERS`).
- New contributors are welcome: open an issue first for anything large (see `CONTRIBUTING.md`).

## Who tests what

Behaviour depends on the plant, so a change to SIP, media, the door, registration or transport is tried on a real
intercom before a release, once per batch of merged PRs:

| Who | Intercom | Tests |
|---|---|---|
| Lollo | 40507 Tab 7S 2-wire, local UDP and cloud | calls, rings, door and actuators, phonebook, voicemail and DND |
| Apeiv | 40515, cloud | cloud rings and calls, card, `/av` with go2rtc, Frigate and Scrypted |
| m4r1k | 40517 2FV2, cloud | HomeKit doorbell, SRTP |

A PR says in its description whether it needs an intercom test, and what it was tested on.

## Reviews and merging

- Every change goes through a PR to `main`: no direct pushes, no force pushes (repository ruleset).
- A PR needs green CI and the `Changelog:` lines in its description (`CONTRIBUTING.md`). Approvals are not
  required while there is one maintainer; the maintainer reviews every PR before merging.
- Merges use a merge commit. The maintainer merges; with Write, Apeiv and m4r1k may merge PRs in their own
  areas that another person has reviewed.
- PRs from other authors are reviewed from the diff and tested in CI; they are not run on a maintainer's machine.

## Releases

Only the maintainer releases: a `release/X.Y.Z` PR (manifest version and the changelog written from the
`Changelog:` lines of the merged PRs), then a GitHub release on the merge commit; the HACS zip is attached by CI.
An urgent fix (door, lost rings, crash at startup, security, a regression of the last release) can go out as a
patch release from the last tag.

## When the maintainer is away

- Issues and PRs keep being triaged by Apeiv and m4r1k; nothing is merged without green CI.
- No releases. A security report goes through a private advisory (`SECURITY.md`) and waits for the maintainer,
  unless it is being exploited: then the people with Write may revert the change that caused it.
- If the maintainer is gone for good, the organisation `ha-vimar` keeps the repository; its owners decide who
  takes over.
