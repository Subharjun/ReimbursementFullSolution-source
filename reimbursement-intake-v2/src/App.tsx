import { useState, useCallback, useEffect } from 'react';
import IntakeForm from './IntakeForm';
import './App.css';

function App() {
  const [darkTheme, setDarkTheme] = useState(false);
  const toggleTheme = useCallback(() => setDarkTheme((d) => !d), []);

  useEffect(() => {
    document.body.className = darkTheme ? 'dark' : 'light';
  }, [darkTheme]);

  return (
    <div className={`app-shell ${darkTheme ? 'dark' : 'light'}`}>
      <IntakeForm darkTheme={darkTheme} onToggleTheme={toggleTheme} />
    </div>
  );
}

export default App;
