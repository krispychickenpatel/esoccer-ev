import { NavLink, Route, Routes } from 'react-router-dom'
import Alerts from './pages/Alerts'
import Picks from './pages/Picks'
import Recs from './pages/Recs'
import Research from './pages/Research'
import Shadow from './pages/Shadow'
import Health from './pages/Health'
import Glossary from './pages/Glossary'
import Backtests from './pages/Backtests'
import Bets from './pages/Bets'
import Dashboard from './pages/Dashboard'
import Matches from './pages/Matches'
import Odds from './pages/Odds'
import Predictions from './pages/Predictions'
import PredictionLab from './pages/PredictionLab'
import Ratings from './pages/Ratings'
import SettingsPage from './pages/Settings'
import FriendPicks from './pages/FriendPicks'
import ProfitReadiness from './pages/ProfitReadiness'

const NAV = [
  ['/', 'Dashboard'], ['/picks', 'Best Picks'], ['/recs', 'Recommendations'],
  ['/bets', 'Bets'], ['/matches', 'Matches'], ['/odds', 'Odds'],
  ['/predictions', 'Predictions'], ['/lab', 'Prediction Lab'], ['/friend-picks', 'Friend Picks'],
  ['/profit-readiness', 'Profit Readiness'],
  ['/backtests', 'Backtests'], ['/ratings', 'Players'],
  ['/shadow', 'Shadow Model'], ['/research', 'Research'], ['/alerts', 'Alerts'],
  ['/health', 'Data Health'], ['/glossary', 'Glossary'], ['/settings', 'Settings'],
] as const

export default function App() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          ESOCCER<span className="tick">▲</span>EV
          <small>RESEARCH TERMINAL · v0.3.6-profit</small>
        </div>
        <nav className="nav">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === '/'}
              className={({ isActive }) => (isActive ? 'active' : '')}>
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/picks" element={<Picks />} />
          <Route path="/recs" element={<Recs />} />
          <Route path="/shadow" element={<Shadow />} />
          <Route path="/research" element={<Research />} />
          <Route path="/health" element={<Health />} />
          <Route path="/glossary" element={<Glossary />} />
          <Route path="/bets" element={<Bets />} />
          <Route path="/matches" element={<Matches />} />
          <Route path="/odds" element={<Odds />} />
          <Route path="/predictions" element={<Predictions />} />
          <Route path="/lab" element={<PredictionLab />} />
          <Route path="/friend-picks" element={<FriendPicks />} />
          <Route path="/profit-readiness" element={<ProfitReadiness />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/ratings" element={<Ratings />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  )
}
