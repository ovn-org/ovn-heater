# How to submit code changes for ovn-heater

Please create github pull requests for the repo.  More details are
included below.

## Before You Start

Before you open PRs, make sure that each commit to be included in the
PR makes sense.  In particular:

  - A given commit should not break anything, even if later
    commits fix the problems that it causes.  The source tree
    should still work after each commit is applied.  (This enables
    `git bisect` to work best.)

  - A commit should make one logical change.  Don't make
    multiple, logically unconnected changes to disparate
    subsystems in a single commit.

  - A commit that adds or removes user-visible features should
    also update the appropriate user documentation or manpages.

## Commit Summary

The summary line of your commit should be in the following format:
`<area>: <summary>`

  - `<area>:` indicates the ovn-heater area to which the
    change applies (often the name of a source file or a
    directory).  You may omit it if the change crosses multiple
    distinct pieces of code.

  - `<summary>` briefly describes the change.

## Commit Description

The body of the commit message should start with a more thorough
description of the change.  This becomes the body of the commit
message, following the subject.  There is no need to duplicate the
summary given in the subject.

Please limit lines in the description to 79 characters in width.

The description should include:

  - The rationale for the change.

  - Design description and rationale (but this might be better
    added as code comments).

  - Testing that you performed (or testing that should be done
    but you could not for whatever reason).

  - Tags (see below).

There is no need to describe what the commit actually changed, if
readers can see it for themselves.

If the commit refers to a commit already in the ovn-heater
repository, please include both the commit number and the subject of
the commit, e.g. 'commit 7bbeecf09bf5 ("cluster_density: Skip build pods
in the startup stage.")'.

## Tags

The description ends with a series of tags, written one to a line as
the last paragraph of the email.  Each tag indicates some property of
the commit in an easily machine-parseable manner.

Examples of common tags follow.

```
Signed-off-by: Author Name <author.name@email.address...>
```

Informally, this indicates that Author Name is the author or
submitter of a commit and has the authority to submit it under
the terms of the license.  The formal meaning is to agree to
the Developer's Certificate of Origin (see below).

If the author and submitter are different, each must sign off.
If the commit has more than one author, all must sign off:

```
Signed-off-by: Author Name <author.name@email.address...>
Signed-off-by: Submitter Name <submitter.name@email.address...>
```

Git can only record a single person as the author of a given patch.
In the rare event that a patch has multiple authors, one must be given
the credit in Git and the others must be credited via `Co-authored-by:`
tags (all co-authors must also sign off):

```
Co-authored-by: Author Name <author.name@email.address...>
```

## Developer's Certificate of Origin

To help track the author of a commit as well as the submission chain,
and be clear that the developer has authority to submit a commit for
inclusion in ovn-heater please sign off your work.  The sign off
certifies the following:

    Developer's Certificate of Origin 1.1

    By making a contribution to this project, I certify that:

    (a) The contribution was created in whole or in part by me and I
        have the right to submit it under the open source license
        indicated in the file; or

    (b) The contribution is based upon previous work that, to the best
        of my knowledge, is covered under an appropriate open source
        license and I have the right under that license to submit that
        work with modifications, whether created in whole or in part
        by me, under the same open source license (unless I am
        permitted to submit under a different license), as indicated
        in the file; or

    (c) The contribution was provided directly to me by some other
        person who certified (a), (b) or (c) and I have not modified
        it.

    (d) I understand and agree that this project and the contribution
        are public and that a record of the contribution (including all
        personal information I submit with it, including my sign-off) is
        maintained indefinitely and may be redistributed consistent with
        this project or the open source license(s) involved.

See also https://developercertificate.org.
