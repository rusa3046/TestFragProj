"""What a pair has to be before anyone gets to read about it.

These five numbers are the product's central editorial rule, and they are
in their own module because two very different callers need them:
`pages.py`, which refuses to render a pair that falls short, and
`resolve/entities.py`, which ranks curation work by how many pairs naming
one mention would carry over the line. A curation queue that used
different numbers from the renderer would be pointing at work that does
not publish.

They lived in `pages.py` until 2026-08-12, which read as if the bar were a
rendering concern. It is not: it is a claim about evidence, and HTML is
downstream of it.
"""

from __future__ import annotations

#: Distinct people who must connect a pair before it gets a page. Below
#: this, "people say this" is one person and an echo.
MIN_COMMENTERS = 3

#: Distinct *creators* those people must span — `source_channel` is the
#: uploading channel, so every video by one YouTuber counts once. Three
#: commenters in one comment section is one conversation, not three
#: observations, and three comment sections belonging to one channel are
#: one audience.
MIN_SOURCES = 2

#: The same two bars, raised, for a house compared against its own line —
#: Layton vs Layton Exclusif, Amber Oud Ruby vs Amber Oud Black Edition.
#:
#: Two bottles from one house whose names overlap are the case entity
#: resolution gets wrong most often, and the failure is silent: the names
#: differ by one word, so a comment about the flanker resolves to the
#: parent about as easily as to the flanker, and the resulting page reads
#: as a house duping itself. `docs/CURATION.md` is largely about not making
#: that merge, and `mention_only_words` exists because it was made once.
#:
#: The evidence is also weaker than it looks. "The Exclusif is better" is
#: the single most common sentence under a flanker video, and it is an
#: opinion about two bottles the same house sells — nobody is choosing
#: between houses, and no shopper is being warned off a fake.
#:
#: So these pairs need five people across three creators rather than three
#: across two. It is a deliberately steep bar for a category that is cheap
#: to produce and easy to get wrong.
MIN_FLANKER_COMMENTERS = 5
MIN_FLANKER_SOURCES = 3

#: Distinct *search queries* those videos must span. Deliberately 1 — i.e.
#: off — because raising it is a product decision that should be made
#: against the measured damage rather than on the argument alone.
#:
#: The argument is good: three `parfums de marly layton dupe` videos
#: satisfy MIN_SOURCES while being three rooms in which the same question
#: was asked of an audience assembled for that question. On the committed
#: corpus, setting this to 2 removes four of the six pairs that publish
#: today. That may mean the edges are weak; it may equally mean the eight
#: seed queries were too narrow. Those call for opposite fixes, and the
#: number cannot tell them apart on its own — so it is measured and shown
#: first, and enforced only once the discovery seeds are broader.
MIN_QUERIES = 1

