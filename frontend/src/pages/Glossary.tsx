const TERMS: { term: string; group: string; def: string }[] = [
  { term: 'Elo', group: 'Ratings', def: 'A running skill score, starts everyone at 1500. Wins pull it up, losses pull it down, and the amount moved depends on how surprising the result was (beating a much stronger player moves your rating more than beating a weaker one).' },
  { term: 'MP', group: 'Ratings', def: 'Matches Played — how many finished games this player has in the database. Below ~25, the Elo number is unreliable; the system down-weights confidence in these cases automatically.' },
  { term: 'Attack / Defense', group: 'Ratings', def: 'Rolling average goals scored (attack) and conceded (defense) over the last 25 matches. Separate from Elo — Elo is about winning, these are about scoring patterns.' },
  { term: 'Form (L10)', group: 'Ratings', def: 'Points per match over the last 10 games (win=3, draw=1, loss=0), scaled to 0–3. Same idea as Elo but reacts faster to recent hot/cold streaks.' },
  { term: 'GF / GA', group: 'Ratings', def: 'Goals For / Goals Against — total or average goals a player has scored / conceded across their match history.' },
  { term: 'Fair odds', group: 'Betting math', def: 'What the odds *should* be if the model\'s probability is correct, with zero bookmaker margin. Fair decimal odds = 1 ÷ model probability.' },
  { term: 'Decimal odds', group: 'Betting math', def: 'European-style odds format. Decimal 2.00 = your stake doubles if you win (a $10 bet returns $20 total). Higher number = bigger underdog.' },
  { term: 'American odds', group: 'Betting math', def: 'The +150 / -140 format US books use. Positive = profit on a $100 bet if it wins (+150 = win $150). Negative = stake needed to win $100 (-140 = bet $140 to win $100).' },
  { term: 'Implied probability', group: 'Betting math', def: 'What the odds say the win chance is, if you ignore the bookmaker\'s built-in margin. Decimal odds of 2.00 imply 50%.' },
  { term: 'De-vig / vig', group: 'Betting math', def: 'Vig (aka juice, hold) is the bookmaker\'s built-in profit margin — implied probabilities across all outcomes always add up to slightly more than 100%. De-vig removes that margin to get the book\'s true estimate of the odds.' },
  { term: 'EV (Expected Value)', group: 'Betting math', def: 'Model probability × decimal odds − 1. Positive EV means the model thinks the bet pays out more than it should, on average, over many repeats. Does not mean any single bet wins — it\'s a long-run average.' },
  { term: 'CLV (Closing Line Value)', group: 'Betting math', def: 'How your bet\'s odds compared to the final odds right before kickoff. Consistently beating the closing line is considered the single best evidence of real, sustainable skill — better than short-term win rate, which is mostly noise.' },
  { term: 'Kelly / Kelly fraction', group: 'Betting math', def: 'A formula for how much to stake based on your edge size and the odds — bigger EV and better odds justify a bigger stake, small or negative edge means stake near zero. "Kelly fraction" (e.g. 0.25) means betting a quarter of what full Kelly recommends, since full Kelly is extremely aggressive and one wrong probability estimate can hurt badly.' },
  { term: 'Wilson CI (confidence interval)', group: 'Statistics', def: 'A range for what your true win rate probably is, given how few bets you\'ve actually settled. 9 wins out of 9 sounds like 100%, but the Wilson interval might say the true rate is anywhere from 70–100% — small samples are less certain than they feel.' },
  { term: 'Calibration', group: 'Statistics', def: 'Checks whether the model is honest: of all picks it said were "60% likely," did about 60% of them actually win? If the model says 60% but only 45% hit, it\'s overconfident.' },
  { term: 'Drift', group: 'Statistics', def: 'Whether recent performance (last 25/50/100 picks) is getting better, worse, or staying flat compared to history. Flags if a previously-working approach has stopped working.' },
  { term: 'Consensus', group: 'Picks', def: 'Whether your friend\'s pick and the system\'s independent model agree on the same side. "Strong consensus" or "friend+model agree" picks are treated with more confidence than either source alone.' },
  { term: 'Reason codes', group: 'Picks', def: 'Short tags explaining why a pick got its status — e.g. ELO_EDGE (model rates one side clearly stronger), STALE_LINE (odds haven\'t moved recently, might be outdated), LIMIT_TOO_LOW (book won\'t accept the stake size needed).' },
  { term: 'Execution window', group: 'Picks', def: 'How long after a match goes live the recommended bet is still considered valid. After this window, a pick is marked MISSED rather than BET, since the odds have likely moved.' },
  { term: 'Data source / verification status', group: 'Data quality', def: 'Every row is tagged with where it came from: LIVE (real, from BetsAPI), SEED (your friend\'s real reconstructed screenshots), DEMO (randomly generated, not real), or MANUAL (you typed/imported it). Verification status separately tracks whether it\'s been reviewed and confirmed.' },
  { term: 'Phase (pre-match / live)', group: 'Data quality', def: 'Whether an odds snapshot was taken before or after kickoff. "Live" phase snapshots are what let the system measure how odds move once a match actually starts.' },
]

const GROUPS = Array.from(new Set(TERMS.map(t => t.group)))

export default function Glossary() {
  return (
    <>
      <h1>Glossary</h1>
      <p className="sub">Every term used across the app, explained once. Ctrl/Cmd+F to search this page.</p>
      {GROUPS.map(g => (
        <div className="card" key={g} style={{ marginBottom: 16 }}>
          <h3>{g}</h3>
          <table>
            <tbody>
              {TERMS.filter(t => t.group === g).map(t => (
                <tr key={t.term}>
                  <td style={{ width: 180, verticalAlign: 'top' }}><b>{t.term}</b></td>
                  <td style={{ whiteSpace: 'normal' }}>{t.def}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  )
}
