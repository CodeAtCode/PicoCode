package com.picocode

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import com.intellij.util.xmlb.XmlSerializerUtil

/**
 * Project-level settings for PicoCode plugin
 * Stores backend server host and port configuration
 */
@State(
    name = "PicoCodeSettings",
    storages = [Storage("picocode.xml")]
)
@Service(Service.Level.PROJECT)
class PicoCodeSettings : PersistentStateComponent<PicoCodeSettings.SettingsState> {
    
    data class SettingsState(
        var serverHost: String = "localhost",
        var serverPort: Int = 8080
    )
    
    private var state = SettingsState()
    
    override fun getState(): SettingsState = state
    
    override fun loadState(state: SettingsState) {
        XmlSerializerUtil.copyBean(state, this.state)
    }
    
    companion object {
        fun getInstance(project: Project): PicoCodeSettings {
            return project.getService(PicoCodeSettings::class.java)
        }
    }
}
