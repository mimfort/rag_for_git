import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Runs from './pages/Runs'
import RunDetail from './pages/RunDetail'
import Quality from './pages/Quality'
import './styles.css'

export default function App() {
  return (
    <BrowserRouter>
      <div className="app">
        <header className="app-header">
          <div className="header-brand">
            <div className="brand-logo">rg</div>
            <div className="brand-text">
              <span className="brand-name">rag_for_git</span>
              <span className="brand-sub">наблюдаемость</span>
            </div>
          </div>

          <nav className="app-nav">
            <NavLink to="/" end className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
              Дашборд
            </NavLink>
            <NavLink to="/runs" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
              Прогоны
            </NavLink>
            <NavLink to="/quality" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
              Качество
            </NavLink>
          </nav>

          <div className="header-status">
            <span className="status-dot pulse" />
            <span className="status-text">live</span>
          </div>
        </header>

        <main className="app-main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/quality" element={<Quality />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
