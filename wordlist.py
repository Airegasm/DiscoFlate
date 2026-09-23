"""
wordlist.py — the answer pool and letter scoring for the Wordle competition.

Kept apart from minigames.py so the list can grow without burying the game
code, and so the scoring is trivially testable on its own. An operator can
override the pool per competition (`words` on the competition row); this is
the fallback, and it is deliberately common, unambiguous vocabulary — a
競 race is no fun if the answer is a word nobody has met.
"""

from __future__ import annotations

# ~320 common five-letter words. No proper nouns, no plurals of four-letter
# words (guessing -S is a cheap strategy), nothing obscure or unpleasant.
WORDS = """
about above actor acute admit adopt adult after again agent agree ahead alarm
album alert alike alive allow alone along alter among anger angle angry ankle
apart apple apply arena argue arise armor aroma array arrow aside asset audio
audit avoid awake award aware badly baker basic basil batch beach beard beast
began begin begun being belly below bench berry birth black blade blame bland
blank blast blaze bleak blend bless blind block blood bloom board boast bonus
boost booth bound brain brake branch brand brave bread break breed brick bride
brief bring brisk broad broke brook brown brush build built bunch burnt burst
cabin cable candy canal canoe cargo carry carve catch cause cease chain chair
chalk charm chart chase cheap check cheek cheer chess chest chief child chill
choir chord chose chunk cider civil claim clash class clean clear clerk cliff
climb cling clock close cloth cloud clown coach coast cocoa colon color comet
comic coral couch cough could count court cover crack craft crane crash crawl
crazy cream creek crept crest cried crime crisp cross crowd crown crumb crush
curve cycle daily dairy dance dealt debut decay delay dense depth diary dirty
dodge doing donor doubt dough dozen draft drain drama drank dream dress dried
drift drill drink drive drove drown eager eagle early earth eight elbow elder
elect elite empty enemy enjoy enter entry equal error essay event every exact
exile exist extra faint fairy faith false fancy fatal fault favor feast fence
ferry fever fiber field fiery fifth fight final first flame flash fleet flesh
flick fling float flock flood floor flour fluid flush focus force forge forth
forty forum found frame fraud fresh fried front frost fruit fully funny galaxy
giant given giver glass gleam globe glory glove going grace grade grain grand
grant grape graph grasp grass grave great greed green greet grief grill grind
groan group grove growl guard guess guest guide guilt habit handy happy harsh
haste hatch haunt heart heavy hedge hello hence hobby honey honor horse hotel
house human humor hurry ideal image imply index inner input irony issue ivory
jelly jewel joint jolly judge juice kneel knife knock known label labor large
laser later laugh layer learn lease least leave legal lemon level lever light
limit linen liver lobby local lodge logic loose lorry lover lower loyal lucky
lunar lunch lying magic major maker march marsh match maybe mayor meant medal
media mercy merge merit metal meter midst might minor minus mixed model moist
money month moral motor mount mouse mouth movie music naked nasty naval nerve
never newly night noble noise north notch novel nurse ocean offer often olive
onion onset opera orbit order organ other ought ounce outer owner ozone paint
panel panic paper party pasta patch pause peace peach pearl pedal penny perch
petal phase phone photo piano piece pilot pinch pitch pivot pixel place plain
plane plant plate plaza plead point polar porch pound power press price pride
prime print prior prize probe proof proud prove pulse punch pupil puppy purse
queen query quest queue quick quiet quilt quite quota radar radio raise rally
ranch range rapid ratio reach react ready realm rebel refer reign relax relay
renew repay reply rider ridge rifle right rigid rinse risky rival river roast
robin robot rocky rough round route royal rugby ruler rumor rural sadly saint
salad salon sandy sauce scale scarf scene scent scope score scout scrap screw
sense serve seven shade shaft shake shall shame shape share shark sharp sheep
sheet shelf shell shift shine shirt shock shoot shore short shout shown sight
silly since siren skill skirt slate sleep slice slide slope small smart smile
smoke snack snake sneak solar solid solve sorry sound south space spare spark
speak speed spell spend spent spice spike spine spite split spoke spoon sport
spray squad stack staff stage stain stair stake stamp stand stare start state
steam steel steep steer stern stick stiff still sting stock stone stood stool
store storm story stove strap straw strip stuck study stuff style sugar suite
sunny super surge sweat sweet swift swing sword table taken talon tarot taste
teach tempo tenor tense thank theft their theme there thick thief thigh thing
think third those three threw throw thumb tidal tiger tight timer title toast
today token tonic tooth topic torch total touch tough towel tower toxic trace
track trade trail train trait trash treat trend trial tribe trick tried troop
trout truck truly trunk trust truth twice twist ultra uncle under union unify
unite unity until upper upset urban urged usage usual vague valid value valve
vapor vault venue verse video vigor villa vinyl viral virus visit vital vivid
vocal voice voter wagon waist waste watch water weary weave wedge weigh weird
whale wheat wheel where which while whirl white whole whose widen widow width
witch woman world worry worse worth would wound woven wrist write wrong yacht
yield young youth zebra
""".split()

# defensive: the pool must only hold clean five-letter entries
WORDS = sorted({w for w in WORDS if len(w) == 5 and w.isalpha()})

HIT, NEAR, MISS = "hit", "near", "miss"


def score_guess(guess: str, answer: str) -> list:
    """Per-letter marks for one guess: HIT (right letter, right place),
    NEAR (in the word, wrong place) or MISS.

    TWO PASSES, and that is the whole trick. Marking NEAR by asking "is this
    letter in the answer" over-reports duplicates: guess CRANE against ERASE
    and the single E gets credited twice. So exact matches are taken first and
    their letters consumed from a pool; only what's left can make a NEAR.
    """
    guess, answer = str(guess).lower(), str(answer).lower()
    n = len(answer)
    marks = [MISS] * len(guess)
    pool: dict[str, int] = {}
    # pass 1 — exact positions claim their letter
    for i, ch in enumerate(guess[:n]):
        if ch == answer[i]:
            marks[i] = HIT
    for i, ch in enumerate(answer):
        if i >= len(guess) or guess[i] != ch:
            pool[ch] = pool.get(ch, 0) + 1
    # pass 2 — the rest may only take what pass 1 left behind
    for i, ch in enumerate(guess[:n]):
        if marks[i] == HIT:
            continue
        if pool.get(ch, 0) > 0:
            marks[i] = NEAR
            pool[ch] -= 1
    return marks


SQUARES = {HIT: "🟩", NEAR: "🟨", MISS: "⬛"}


def render_row(guess: str, answer: str) -> str:
    """The shareable emoji row for one guess."""
    return "".join(SQUARES[m] for m in score_guess(guess, answer))


def pick(pool=None) -> str:
    """One answer. `pool` lets a competition ship its own vocabulary."""
    import random
    words = [str(w).strip().lower() for w in (pool or []) if str(w).strip()]
    words = [w for w in words if len(w) == 5 and w.isalpha()] or WORDS
    return random.choice(words)
