"""
Backoffice Kernel - Platform Core v1.2.1 (Final Polish)
========================================================

Central registry and control layer for backoffice system.

Features:
- Module registry and discovery
- Feature flags with rollout support
- Version management (auto-detected)
- System health validation
- Execution mode control
- Event bus foundation

v1.2.1 Final Polish:
- MD5 hash for fallback rollout (consistency)
- Enhanced config error logging (observability)
- Config reload capability (future-ready)
- Event bus async note (documented)
"""

import logging
import sys
import os
import json
import hashlib
from typing import Dict, Any, Optional, List, Callable
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# =============================================================================
# INITIALIZATION GUARD
# =============================================================================

_KERNEL_INITIALIZED = False


# =============================================================================
# VERSION MANAGEMENT
# =============================================================================

BACKOFFICE_VERSION = "2.0.0-stable"
KERNEL_VERSION = "1.2.1"

# Auto-detect Python version
PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

# Build date - auto-generated from file metadata
try:
    _kernel_file = os.path.abspath(__file__)
    _build_timestamp = os.path.getmtime(_kernel_file)
    BUILD_DATE = datetime.fromtimestamp(_build_timestamp).strftime("%Y-%m-%d")
except Exception:
    BUILD_DATE = datetime.now().strftime("%Y-%m-%d")


def get_version_info() -> Dict[str, str]:
    """Get complete version information"""
    return {
        "backoffice_version": BACKOFFICE_VERSION,
        "kernel_version": KERNEL_VERSION,
        "build_date": BUILD_DATE,
        "python_version": PYTHON_VERSION,
        "architecture": "modular-platform"
    }


# =============================================================================
# EXECUTION MODE
# =============================================================================

class ExecutionMode(Enum):
    """System execution mode"""
    DEVELOPMENT = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


# Default mode - can be overridden by ENV variable
_ENV_MODE = os.getenv("BACKOFFICE_MODE", "dev").lower()
if _ENV_MODE == "production":
    CURRENT_MODE = ExecutionMode.PRODUCTION
elif _ENV_MODE == "staging":
    CURRENT_MODE = ExecutionMode.STAGING
else:
    CURRENT_MODE = ExecutionMode.DEVELOPMENT


def set_execution_mode(mode: ExecutionMode):
    """Set global execution mode"""
    global CURRENT_MODE
    CURRENT_MODE = mode
    logger.info(f"[KERNEL] Execution mode set to: {mode.value}")


def get_execution_mode() -> ExecutionMode:
    """Get current execution mode"""
    return CURRENT_MODE


def is_production() -> bool:
    """Check if running in production mode"""
    return CURRENT_MODE == ExecutionMode.PRODUCTION


def is_development() -> bool:
    """Check if running in development mode"""
    return CURRENT_MODE == ExecutionMode.DEVELOPMENT


# =============================================================================
# FEATURE FLAGS
# =============================================================================

@dataclass
class Feature:
    """Feature flag definition"""
    name: str
    enabled: bool = True
    description: str = ""
    min_version: str = "2.0.0"
    rollout_percentage: int = 100  # 0-100: gradual rollout support
    dependencies: List[str] = field(default_factory=list)


class FeatureRegistry:
    """
    Central feature flag registry with rollout support.
    
    Supports:
    - ENV variable overrides (FEATURE_<name>=true/false)
    - JSON config file (feature_flags.json)
    - Stable MD5-based rollout hashing
    - Config hot reload (reload_config method)
    """
    
    def __init__(self, config_path: Optional[str] = None):
        self._features: Dict[str, Feature] = {}
        self._user_rollout_cache: Dict[str, bool] = {}
        self._config_path = config_path or os.getenv("FEATURE_CONFIG_PATH")
        
        self._register_default_features()
        self._load_config_overrides()
        self._load_env_overrides()
    
    def _register_default_features(self):
        """Register default backoffice features"""
        
        # Core features (always enabled)
        self.register(Feature(
            name="order_management",
            enabled=True,
            description="Core order management functionality",
            rollout_percentage=100
        ))
        
        self.register(Feature(
            name="backoffice_ui",
            enabled=True,
            description="Main backoffice UI with 6 tabs",
            rollout_percentage=100
        ))
        
        # Optional features (can be toggled)
        self.register(Feature(
            name="production_module",
            enabled=True,
            description="Production floor tracking and job cards",
            rollout_percentage=100
        ))
        
        self.register(Feature(
            name="supplier_panel",
            enabled=True,
            description="Supplier PO management",
            rollout_percentage=100,
            dependencies=["backoffice_ui"]
        ))
        
        self.register(Feature(
            name="billing_gate",
            enabled=True,
            description="Controlled billing interface",
            rollout_percentage=100,
            dependencies=["backoffice_ui"]
        ))
        
        self.register(Feature(
            name="sidebar_dashboard",
            enabled=True,
            description="Real-time status sidebar",
            rollout_percentage=100,
            dependencies=["backoffice_ui"]
        ))
        
        # Experimental features (can be disabled)
        self.register(Feature(
            name="event_timeline",
            enabled=False,  # Requires order_events table
            description="Activity timeline in sidebar",
            rollout_percentage=0,
            dependencies=["sidebar_dashboard"]
        ))
        
        self.register(Feature(
            name="advanced_analytics",
            enabled=False,
            description="Advanced analytics and reporting",
            rollout_percentage=50  # Gradual rollout example
        ))
    
    def _load_config_overrides(self):
        """Load feature overrides from JSON config file"""
        if not self._config_path:
            logger.debug("[KERNEL] No config path set - skipping file-based config")
            return
        
        try:
            config_file = Path(self._config_path)
            if not config_file.exists():
                logger.debug(f"[KERNEL] Config file not found: {self._config_path}")
                return
            
            with open(config_file, 'r') as f:
                config = json.load(f)
            
            features_config = config.get("features", {})
            for name, settings in features_config.items():
                if name in self._features:
                    if "enabled" in settings:
                        self._features[name].enabled = bool(settings["enabled"])
                    if "rollout_percentage" in settings:
                        self._features[name].rollout_percentage = int(settings["rollout_percentage"])
                    logger.info(f"[KERNEL] Feature '{name}' configured from {self._config_path}")
                else:
                    logger.warning(f"[KERNEL] Unknown feature '{name}' in config file")
        
        except json.JSONDecodeError as e:
            logger.error(
                f"[KERNEL] Failed to parse config file: {self._config_path}\n"
                f"JSON syntax error at line {e.lineno}, column {e.colno}: {e.msg}\n"
                f"Check your JSON syntax - common issues: trailing commas, unquoted keys, single quotes"
            )
        except Exception as e:
            logger.error(
                f"[KERNEL] Failed to load config file: {self._config_path}\n"
                f"Error: {type(e).__name__}: {e}"
            )
    
    def _load_env_overrides(self):
        """Load feature overrides from environment variables"""
        for name in self._features.keys():
            env_var = f"FEATURE_{name.upper()}"
            env_value = os.getenv(env_var)
            
            if env_value is not None:
                if env_value.lower() in ("true", "1", "yes", "on"):
                    self._features[name].enabled = True
                    logger.info(f"[KERNEL] Feature '{name}' enabled via ENV ({env_var})")
                elif env_value.lower() in ("false", "0", "no", "off"):
                    self._features[name].enabled = False
                    logger.info(f"[KERNEL] Feature '{name}' disabled via ENV ({env_var})")
            
            # Check rollout override
            env_rollout = os.getenv(f"{env_var}_ROLLOUT")
            if env_rollout is not None:
                try:
                    rollout = int(env_rollout)
                    if 0 <= rollout <= 100:
                        self._features[name].rollout_percentage = rollout
                        logger.info(f"[KERNEL] Feature '{name}' rollout set to {rollout}% via ENV ({env_var}_ROLLOUT)")
                    else:
                        logger.warning(f"[KERNEL] Invalid rollout value for {env_var}_ROLLOUT: {env_rollout} (must be 0-100)")
                except ValueError:
                    logger.warning(f"[KERNEL] Invalid rollout value for {env_var}_ROLLOUT: {env_rollout} (not an integer)")
    
    def reload_config(self):
        """
        Reload configuration from file and environment.
        
        Useful for:
        - Admin-triggered config refresh
        - Hot reload without restart
        - Testing config changes
        
        NOTE: Clears rollout cache to re-evaluate assignments
        """
        logger.info("[KERNEL] Reloading feature configuration...")
        
        # Clear rollout cache
        self._user_rollout_cache.clear()
        
        # Reload from file and env
        self._load_config_overrides()
        self._load_env_overrides()
        
        logger.info("[KERNEL] Configuration reloaded successfully")
    
    def register(self, feature: Feature):
        """Register a feature"""
        self._features[feature.name] = feature
        logger.debug(f"[KERNEL] Feature registered: {feature.name} (enabled={feature.enabled}, rollout={feature.rollout_percentage}%)")
    
    def is_enabled(self, feature_name: str, user_id: Optional[str] = None) -> bool:
        """
        Check if feature is enabled for a given user (with rollout support).
        
        Args:
            feature_name: Name of the feature
            user_id: Optional user ID for gradual rollout (uses stable MD5 hash)
        
        Returns:
            True if feature is enabled for this user
        """
        if feature_name not in self._features:
            logger.warning(f"[KERNEL] Unknown feature: {feature_name}")
            return False
        
        feature = self._features[feature_name]
        
        # Check if feature is globally disabled
        if not feature.enabled:
            return False
        
        # Check dependencies
        if feature.dependencies:
            for dep in feature.dependencies:
                if not self.is_enabled(dep, user_id):
                    logger.debug(f"[KERNEL] Feature {feature_name} disabled: dependency {dep} not enabled")
                    return False
        
        # Check rollout percentage
        if feature.rollout_percentage < 100:
            return self._is_in_rollout(feature_name, feature.rollout_percentage, user_id)
        
        return True
    
    def _is_in_rollout(self, feature_name: str, percentage: int, user_id: Optional[str]) -> bool:
        """
        Determine if user is in rollout group using stable MD5 hashing.
        
        Production-safe: Uses MD5 for all cases (user-based and fallback).
        """
        # Production guard: warn if no user_id provided
        if user_id is None:
            if is_production():
                logger.warning(
                    f"[KERNEL] Feature rollout '{feature_name}' without user_id in PRODUCTION. "
                    "This causes inconsistent UX. Provide user_id for stable assignment."
                )
            # Fallback: Use MD5 hash of feature name for consistent session-based assignment
            # v1.2.1 improvement: MD5 instead of Python hash() for consistency
            fallback_key = f"{feature_name}:session_fallback"
            hash_digest = hashlib.md5(fallback_key.encode()).hexdigest()
            hash_val = int(hash_digest[:8], 16) % 100
            return hash_val < percentage
        
        # Cache key for consistent results per user
        cache_key = f"{feature_name}:{user_id}"
        
        if cache_key in self._user_rollout_cache:
            return self._user_rollout_cache[cache_key]
        
        # Stable MD5-based assignment (production-safe)
        hash_digest = hashlib.md5(cache_key.encode()).hexdigest()
        # Use first 8 hex chars, convert to int, mod 100
        hash_val = int(hash_digest[:8], 16) % 100
        in_rollout = hash_val < percentage
        
        self._user_rollout_cache[cache_key] = in_rollout
        logger.debug(f"[KERNEL] Rollout for {feature_name}:{user_id} = {in_rollout} (hash={hash_val}, threshold={percentage})")
        
        return in_rollout
    
    def enable(self, feature_name: str):
        """Enable a feature"""
        if feature_name in self._features:
            self._features[feature_name].enabled = True
            logger.info(f"[KERNEL] Feature enabled: {feature_name}")
    
    def disable(self, feature_name: str):
        """Disable a feature"""
        if feature_name in self._features:
            self._features[feature_name].enabled = False
            logger.info(f"[KERNEL] Feature disabled: {feature_name}")
    
    def set_rollout(self, feature_name: str, percentage: int):
        """Set rollout percentage for gradual feature release"""
        if feature_name in self._features:
            if 0 <= percentage <= 100:
                self._features[feature_name].rollout_percentage = percentage
                # Clear cache when rollout changes
                self._user_rollout_cache.clear()
                logger.info(f"[KERNEL] Feature {feature_name} rollout set to {percentage}%")
            else:
                raise ValueError("Rollout percentage must be between 0 and 100")
    
    def get_all(self) -> Dict[str, Feature]:
        """Get all registered features"""
        return self._features.copy()
    
    def get_enabled(self, user_id: Optional[str] = None) -> List[str]:
        """Get list of enabled features for a user"""
        return [name for name in self._features.keys() if self.is_enabled(name, user_id)]
    
    def save_config(self, config_path: Optional[str] = None):
        """Save current feature configuration to JSON file"""
        path = config_path or self._config_path
        if not path:
            raise ValueError("No config path specified")
        
        config = {
            "features": {
                name: {
                    "enabled": feat.enabled,
                    "rollout_percentage": feat.rollout_percentage,
                    "description": feat.description
                }
                for name, feat in self._features.items()
            },
            "metadata": {
                "saved_at": datetime.now().isoformat(),
                "kernel_version": KERNEL_VERSION
            }
        }
        
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)
        
        logger.info(f"[KERNEL] Feature config saved to {path}")


# Global feature registry
FEATURES = FeatureRegistry()


# =============================================================================
# MODULE REGISTRY
# =============================================================================

@dataclass
class Module:
    """Module definition"""
    name: str
    description: str
    entry_point: str
    version: str = "2.0.0"
    dependencies: List[str] = field(default_factory=list)
    loaded: bool = False
    load_time: Optional[datetime] = None
    error: Optional[str] = None


class ModuleRegistry:
    """Central module registry"""
    
    def __init__(self):
        self._modules: Dict[str, Module] = {}
        self._register_default_modules()
    
    def _register_default_modules(self):
        """Register default backoffice modules"""
        
        self.register(Module(
            name="backoffice",
            description="Main backoffice controller",
            entry_point="modules.backoffice.backoffice",
            dependencies=[]
        ))
        
        self.register(Module(
            name="backoffice_ui",
            description="Backoffice UI components",
            entry_point="modules.backoffice.backoffice_ui",
            dependencies=["backoffice"]
        ))
        
        self.register(Module(
            name="production_page",
            description="Production floor interface",
            entry_point="modules.backoffice.production_page",
            dependencies=[]
        ))
        
        self.register(Module(
            name="supplier_panel",
            description="Supplier order management",
            entry_point="modules.backoffice.supplier_panel",
            dependencies=["backoffice_ui"]
        ))
        
        self.register(Module(
            name="billing_gate",
            description="Billing control interface",
            entry_point="modules.backoffice.billing_gate",
            dependencies=["backoffice_ui"]
        ))
        
        self.register(Module(
            name="backoffice_sidebar",
            description="Real-time sidebar dashboard",
            entry_point="modules.backoffice.backoffice_sidebar",
            dependencies=["backoffice_ui"]
        ))
    
    def register(self, module: Module):
        """Register a module"""
        self._modules[module.name] = module
        logger.debug(f"[KERNEL] Module registered: {module.name}")
    
    def mark_loaded(self, module_name: str, success: bool = True, error: Optional[str] = None):
        """Mark module as loaded"""
        if module_name in self._modules:
            self._modules[module_name].loaded = success
            self._modules[module_name].load_time = datetime.now()
            self._modules[module_name].error = error
            
            if success:
                logger.info(f"[KERNEL] Module loaded: {module_name}")
            else:
                logger.error(f"[KERNEL] Module load failed: {module_name} - {error}")
    
    def get_all(self) -> Dict[str, Module]:
        """Get all registered modules"""
        return self._modules.copy()
    
    def get_loaded(self) -> List[str]:
        """Get list of loaded modules"""
        return [name for name, mod in self._modules.items() if mod.loaded]
    
    def get_failed(self) -> Dict[str, str]:
        """Get modules that failed to load"""
        return {name: mod.error for name, mod in self._modules.items() 
                if not mod.loaded and mod.error}


# Global module registry
MODULES = ModuleRegistry()


# =============================================================================
# SYSTEM HEALTH
# =============================================================================

@dataclass
class HealthCheck:
    """Health check result"""
    component: str
    status: str  # "healthy", "degraded", "unhealthy"
    message: str
    timestamp: datetime = field(default_factory=datetime.now)


class SystemHealth:
    """System health validator"""
    
    @staticmethod
    def check_modules() -> HealthCheck:
        """Check if all modules loaded successfully"""
        failed = MODULES.get_failed()
        
        if not failed:
            return HealthCheck(
                component="modules",
                status="healthy",
                message=f"All {len(MODULES.get_loaded())} modules loaded"
            )
        else:
            return HealthCheck(
                component="modules",
                status="unhealthy",
                message=f"{len(failed)} modules failed: {', '.join(failed.keys())}"
            )
    
    @staticmethod
    def check_features() -> HealthCheck:
        """Check feature status"""
        enabled = FEATURES.get_enabled()
        total = len(FEATURES.get_all())
        
        return HealthCheck(
            component="features",
            status="healthy",
            message=f"{len(enabled)}/{total} features enabled"
        )
    
    @staticmethod
    def run_all_checks() -> List[HealthCheck]:
        """Run all health checks"""
        checks = [
            SystemHealth.check_modules(),
            SystemHealth.check_features()
        ]
        return checks
    
    @staticmethod
    def is_healthy() -> bool:
        """Check if system is healthy"""
        checks = SystemHealth.run_all_checks()
        return all(check.status == "healthy" for check in checks)


# =============================================================================
# SELF-TEST
# =============================================================================

def self_test() -> Dict[str, Any]:
    """
    Run system self-test.
    Called on startup or via health endpoint.
    """
    
    logger.info("[KERNEL] Running self-test...")
    
    results = {
        "version": get_version_info(),
        "mode": CURRENT_MODE.value,
        "modules": {
            "loaded": MODULES.get_loaded(),
            "failed": MODULES.get_failed()
        },
        "features": {
            "enabled": FEATURES.get_enabled(),
            "total": len(FEATURES.get_all())
        },
        "health_checks": [
            {
                "component": check.component,
                "status": check.status,
                "message": check.message
            }
            for check in SystemHealth.run_all_checks()
        ],
        "healthy": SystemHealth.is_healthy(),
        "timestamp": datetime.now().isoformat()
    }
    
    logger.info(f"[KERNEL] Self-test complete: healthy={results['healthy']}")
    
    return results


# =============================================================================
# EVENT BUS (Foundation)
# =============================================================================

class EventBus:
    """
    Simple event bus for backoffice events.
    Foundation for future automation (WhatsApp, SLA tracking, etc.)
    
    CURRENT: Synchronous event processing
    
    FUTURE v2.0: Async queue support for non-blocking event processing
    - Will use asyncio.Queue or threading.Queue
    - Prevents blocking main thread on slow listeners
    - Enables parallel event processing
    - Required for: WhatsApp notifications, email alerts, external API calls
    
    For now: Synchronous is fine for internal event logging and simple automation
    """
    
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
    
    def emit(self, event_type: str, data: Dict[str, Any]):
        """
        Emit an event (currently synchronous).
        
        NOTE: Blocks until all listeners complete. For async needs, see v2.0 roadmap.
        """
        logger.debug(f"[KERNEL] Event emitted: {event_type}")
        
        if event_type in self._listeners:
            for listener in self._listeners[event_type]:
                try:
                    listener(event_type, data)
                except Exception as e:
                    logger.error(f"[KERNEL] Event listener failed for {event_type}: {e}")
    
    def on(self, event_type: str, listener: Callable):
        """Register event listener"""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(listener)
        logger.debug(f"[KERNEL] Listener registered for: {event_type}")
    
    def clear_listeners(self, event_type: Optional[str] = None):
        """Clear all listeners or listeners for specific event"""
        if event_type:
            self._listeners.pop(event_type, None)
        else:
            self._listeners.clear()


# Global event bus
EVENTS = EventBus()


# =============================================================================
# KERNEL INITIALIZATION
# =============================================================================

def initialize_kernel(mode: Optional[ExecutionMode] = None) -> Dict[str, Any]:
    """
    Initialize backoffice kernel.
    Call this once at application startup.
    
    Protected against double initialization.
    
    Args:
        mode: Execution mode (dev/staging/production)
    
    Returns:
        Self-test results
    """
    
    global _KERNEL_INITIALIZED
    
    # Guard against double initialization
    if _KERNEL_INITIALIZED:
        logger.warning("[KERNEL] Kernel already initialized - skipping")
        return self_test()
    
    if mode:
        set_execution_mode(mode)
    
    logger.info("=" * 60)
    logger.info(f"[KERNEL] Initializing backoffice kernel v{KERNEL_VERSION}")
    logger.info(f"[KERNEL] Backoffice version: {BACKOFFICE_VERSION}")
    logger.info(f"[KERNEL] Python version: {PYTHON_VERSION}")
    logger.info(f"[KERNEL] Build date: {BUILD_DATE}")
    logger.info(f"[KERNEL] Execution mode: {CURRENT_MODE.value}")
    
    # Log enabled features
    enabled_features = FEATURES.get_enabled()
    logger.info(f"[KERNEL] Features enabled: {', '.join(enabled_features)}")
    logger.info("=" * 60)
    
    # Run self-test
    test_results = self_test()
    
    if not test_results["healthy"]:
        logger.warning("[KERNEL] System health check failed!")
        for check in test_results["health_checks"]:
            if check["status"] != "healthy":
                logger.warning(f"[KERNEL] {check['component']}: {check['message']}")
    
    _KERNEL_INITIALIZED = True
    logger.info("[KERNEL] Kernel initialization complete")
    
    return test_results


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Version
    "BACKOFFICE_VERSION",
    "KERNEL_VERSION",
    "PYTHON_VERSION",
    "get_version_info",
    
    # Execution Mode
    "ExecutionMode",
    "set_execution_mode",
    "get_execution_mode",
    "is_production",
    "is_development",
    
    # Feature Flags
    "Feature",
    "FeatureRegistry",
    "FEATURES",
    
    # Module Registry
    "Module",
    "ModuleRegistry",
    "MODULES",
    
    # System Health
    "HealthCheck",
    "SystemHealth",
    "self_test",
    
    # Event Bus
    "EventBus",
    "EVENTS",
    
    # Initialization
    "initialize_kernel"
]
