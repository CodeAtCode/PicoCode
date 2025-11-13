package com.picocode

import com.intellij.openapi.options.Configurable
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import javax.swing.JComponent
import javax.swing.JPanel

/**
 * Project settings configurable for PicoCode plugin
 * Provides UI to configure backend server host and port
 */
class PicoCodeConfigurable(private val project: Project) : Configurable {
    
    private val hostField = JBTextField(20)
    private val portField = JBTextField(10)
    private var settingsPanel: JPanel? = null
    
    override fun getDisplayName(): String = "PicoCode"
    
    override fun createComponent(): JComponent {
        val settings = PicoCodeSettings.getInstance(project)
        val state = settings.state
        
        // Initialize fields with current settings
        hostField.text = state.serverHost
        portField.text = state.serverPort.toString()
        
        // Create the settings panel with form layout
        settingsPanel = FormBuilder.createFormBuilder()
            .addLabeledComponent(JBLabel("Backend Host:"), hostField, 1, false)
            .addLabeledComponent(JBLabel("Backend Port:"), portField, 1, false)
            .addComponentFillVertically(JPanel(), 0)
            .panel
        
        return settingsPanel!!
    }
    
    override fun isModified(): Boolean {
        val settings = PicoCodeSettings.getInstance(project)
        val state = settings.state
        
        val hostModified = hostField.text != state.serverHost
        val portModified = portField.text.toIntOrNull() != state.serverPort
        
        return hostModified || portModified
    }
    
    override fun apply() {
        val settings = PicoCodeSettings.getInstance(project)
        val state = settings.state
        
        state.serverHost = hostField.text.trim().ifEmpty { "localhost" }
        state.serverPort = portField.text.trim().toIntOrNull() ?: 8080
        
        settings.loadState(state)
    }
    
    override fun reset() {
        val settings = PicoCodeSettings.getInstance(project)
        val state = settings.state
        
        hostField.text = state.serverHost
        portField.text = state.serverPort.toString()
    }
}
