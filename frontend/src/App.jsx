import { useState } from 'react'
import { ProjectSelector } from './components/ProjectSelector.jsx'
import { Workspace } from './components/Workspace.jsx'

export default function App() {
  const [projectId, setProjectId] = useState(null)
  const [initialSourcePath, setInitialSourcePath] = useState('')
  const [initialManualUpdate, setInitialManualUpdate] = useState(true)

  const openProject = (id) => {
    setInitialSourcePath('')
    setInitialManualUpdate(true)
    setProjectId(id)
  }

  const openNewProject = (id, sourcePath = '') => {
    setInitialSourcePath(sourcePath)
    setInitialManualUpdate(true)
    setProjectId(id)
  }

  const openEvolutionProject = (id, sourcePath = '', manualUpdate = true) => {
    setInitialSourcePath(sourcePath)
    setInitialManualUpdate(manualUpdate)
    setProjectId(id)
  }

  const goBack = () => {
    setProjectId(null)
    setInitialSourcePath('')
    setInitialManualUpdate(true)
  }

  if (projectId) {
    return (
      <Workspace
        projectId={projectId}
        initialSourcePath={initialSourcePath}
        initialManualUpdate={initialManualUpdate}
        onBack={goBack}
      />
    )
  }

  return (
    <ProjectSelector
      onSelectProject={openProject}
      onNewProject={openNewProject}
      onEvolutionProject={openEvolutionProject}
    />
  )
}
