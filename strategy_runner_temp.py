#!/usr/bin/env python3
"""
Strategy Runner for Real Trading
This script loads configuration and executes the specified strategy
"""

import sys
import json
import os
import importlib.util
import traceback
from pathlib import Path
from logger import logger_access, logger_error
from utils import get_arg
from constants import set_constants, get_constants

def configure_matplotlib_for_notebook():
    """Configure matplotlib for notebook execution (plots will be embedded in notebook)"""
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        
        # Set matplotlib backend to Agg (non-interactive) for headless execution
        matplotlib.use('Agg')
        
        # Configure default parameters for better quality
        plt.rcParams['figure.dpi'] = 150
        plt.rcParams['savefig.dpi'] = 150
        plt.rcParams['savefig.bbox'] = 'tight'
        plt.rcParams['savefig.facecolor'] = 'white'
        
        logger_access.info("✅ Matplotlib configured for notebook execution")
            
    except ImportError:
        logger_access.error("⚠️ Matplotlib not available")
    except Exception as e:
        logger_access.error(f"⚠️ Failed to configure matplotlib: {e}")

def load_config(config_path):
    """Load strategy configuration from JSON file"""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        logger_access.info(f"✅ Configuration loaded successfully")
        logger_access.info(f"📊 Session: {config.get('session_key', 'N/A')}")
        logger_access.info(f"🎯 Strategy: {config.get('strategy_name', 'N/A')}")
        logger_access.info(f"🏦 Exchange: {config.get('exchange', 'N/A')}")
        logger_access.info(f"💰 Initial Balance: ${config.get('initial_balance', 0):,.2f}")
        return config
    except Exception as e:
        logger_access.error(f"❌ Error loading config: {e}")
        return None

def find_strategy_directory(strategy_name):
    """Find the strategy directory by name with exact-match priority.
    - Prefer exact directory name match (case-insensitive)
    - If no exact match, allow a single unambiguous partial match
    - If multiple partial matches, return None to avoid ambiguity
    """
    strategies_dir = Path(__file__).parent / "strategies"

    if not strategies_dir.exists():
        logger_access.info(f"❌ Strategies directory not found: {strategies_dir}")
        return None

    target = strategy_name.lower().strip()
    exact_matches = []
    partial_matches = []

    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir():
            continue

        dir_name_lower = strategy_dir.name.lower()

        # Only consider directories that contain executables (py or ipynb)
        python_files = list(strategy_dir.glob("*.py"))
        notebook_files = list(strategy_dir.glob("*.ipynb"))
        if not python_files and not notebook_files:
            continue

        if dir_name_lower == target:
            exact_matches.append((strategy_dir, python_files, notebook_files))
        elif target in dir_name_lower:
            partial_matches.append((strategy_dir, python_files, notebook_files))

    def log_and_return(entry, match_type: str):
        d, py_files, nb_files = entry
        logger_access.info(f"✅ Found {match_type} strategy directory: {d}")
        if py_files:
            logger_access.info(f"📁 Contains {len(py_files)} Python files")
        if nb_files:
            logger_access.info(f"📓 Contains {len(nb_files)} Jupyter notebooks")
        return d

    if exact_matches:
        return log_and_return(exact_matches[0], "exact")

    if len(partial_matches) == 1:
        return log_and_return(partial_matches[0], "partial")

    if len(partial_matches) > 1:
        names = ", ".join(d.name for d, _, _ in partial_matches)
        logger_access.info(f"⚠️ Multiple strategy directories match '{strategy_name}': {names}")
        logger_access.info("💡 Please set an exact strategy_name in the config or use STRATEGY_DIR env var")
        return None

    logger_access.info(f"❌ Strategy directory not found for: {strategy_name}")
    return None

def find_all_python_files(strategy_dir):
    """Find all Python files in the strategy directory"""
    python_files = []
    for file in strategy_dir.glob("*.py"):
        if not file.name.startswith("__"):
            python_files.append(file)
    return python_files

def find_all_notebook_files(strategy_dir):
    """Find all Jupyter notebook files in the strategy directory"""
    notebook_files = []
    for file in strategy_dir.glob("*.ipynb"):
        if not file.name.startswith("__"):
            notebook_files.append(file)
    return notebook_files

def is_valid_notebook(notebook_path):
    """Check if a file is a valid Jupyter notebook"""
    try:
        import json
        with open(notebook_path, 'r') as f:
            notebook = json.load(f)
        
        # Check for basic notebook structure
        if 'nbformat' not in notebook or 'cells' not in notebook:
            return False
        return True
    except Exception as e:
        logger_access.error(f"⚠️  Invalid notebook {notebook_path}: {e}")
        return False

def execute_notebook_file(notebook_path, config):
    """Execute a Jupyter notebook with the given configuration"""
    try:
        logger_access.info(f"📓 Executing notebook file: {notebook_path}")
        
        # Check if required packages are available
        try:
            import nbformat
            from nbconvert.preprocessors import ExecutePreprocessor
        except ImportError as e:
            logger_access.error(f"❌ Required packages not installed: {e}")
            logger_access.error("💡 Please install: pip install nbformat nbconvert jupyter")
            return False
        
        # Configure matplotlib for notebook execution
        configure_matplotlib_for_notebook()
        
        # Set environment variables for the notebook
        os.environ['STRATEGY_SESSION_KEY'] = config.get('session_key', '')
        os.environ['STRATEGY_API_KEY'] = config.get('api_key', '')
        os.environ['STRATEGY_API_SECRET'] = config.get('api_secret', '')
        os.environ['STRATEGY_EXCHANGE'] = config.get('exchange', '')
        os.environ['STRATEGY_INITIAL_BALANCE'] = str(config.get('initial_balance', 0))
        os.environ['STRATEGY_CONFIG_PATH'] = config.get('config_path', '')
        
        # Load and execute notebook
        with open(notebook_path, 'r') as f:
            nb = nbformat.read(f, as_version=4)
        
        # Execute the notebook
        ep = ExecutePreprocessor(timeout=600, kernel_name='python3')
        ep.preprocess(nb, {'metadata': {'path': str(notebook_path.parent)}})
        
        # Save the executed notebook (avoid double "executed_" prefix)
        notebook_name = notebook_path.name
        if notebook_name.startswith("executed_"):
            # If already has executed_ prefix, use the original name
            output_path = notebook_path.parent / notebook_name
        else:
            # Add executed_ prefix to original name
            output_path = notebook_path.parent / f"executed_{notebook_name}"
        
        # Remove existing executed file if it exists
        if output_path.exists():
            output_path.unlink()
            logger_access.info(f"🗑️  Removed existing executed file: {output_path}")
        
        # Create new executed file
        with open(output_path, 'w') as f:
            nbformat.write(nb, f)
        
        logger_access.info(f"✅ Notebook executed successfully! Output saved to: {output_path}")
        return True
        
    except Exception as e:
        logger_access.error(f"❌ Error executing notebook {notebook_path}: {e}")
        logger_access.error(f"📋 Traceback:\n{traceback.format_exc()}")
        return False

def execute_strategy_file(script_path, config):
    """Execute a single strategy script with the given configuration"""
    try:
        logger_access.info(f"🚀 Executing strategy file: {script_path}")
        
        # Configure matplotlib for notebook execution
        configure_matplotlib_for_notebook()
        
        # Load the strategy module dynamically
        module_name = f"strategy_module_{script_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        strategy_module = importlib.util.module_from_spec(spec)
        
        # Add the strategy directory to sys.path so imports work
        strategy_dir = str(script_path.parent)
        if strategy_dir not in sys.path:
            sys.path.insert(0, strategy_dir)
        
        ## set argv
        args = sys.argv[1:]
        if len(args) >= 7:
            set_constants(args)
        logger_access.info(f"Parameters zzs: {args}")

        if config.get('paper_trading', False):
            logger_access.info("📝 Paper trading mode enabled")
             
        # Execute the module
        try:
            spec.loader.exec_module(strategy_module)
        except Exception as e:
            logger_access.error(f"❌ Failed to load strategy module: {e}")
            logger_access.error(f"📋 Traceback:\n{traceback.format_exc()}")

        # Look for main function or strategy class
        if hasattr(strategy_module, 'main'):
            logger_access.info("📞 Calling main() function...")
            
            # Check if this is paper trading mode
            if config.get('paper_trading', False):
                logger_access.info("📝 Running strategy in paper trading mode")
            
            # Check if this is continuous mode
            if config.get('continuous_mode', False):
                logger_access.info("🔄 Running strategy in continuous mode (may run indefinitely)")
            
            try:
                result = strategy_module.main()
                logger_access.info(f"✅ Strategy file execution completed with result: {result}")
                return True
            except KeyboardInterrupt:
                logger_access.info("🛑 Strategy execution interrupted by user")
                return True
            except Exception as e:
                logger_access.error(f"❌ Strategy main() function failed: {e}")
                logger_access.error(f"📋 Traceback:\n{traceback.format_exc()}")
                return False
        else:
            logger_access.info("⚠️  No main() function found, executing module directly...")
            logger_access.info("✅ Strategy module executed successfully")
            return True
            
    except Exception as e:
        logger_access.error(f"❌ Error executing strategy file {script_path}: {e}")
        logger_access.error(f"📋 Traceback:\n{traceback.format_exc()}")
        return False

def execute_all_strategies(strategy_dir, config):
    """Execute all Python files and Jupyter notebooks in the strategy directory"""
    python_files = find_all_python_files(strategy_dir)
    notebook_files = find_all_notebook_files(strategy_dir)
    
    # Filter valid notebooks
    valid_notebooks = []
    for notebook in notebook_files:
        if is_valid_notebook(notebook):
            valid_notebooks.append(notebook)
        else:
            logger_access.info(f"⚠️  Skipping invalid notebook: {notebook.name}")
    
    total_files = len(python_files) + len(valid_notebooks)
    
    if total_files == 0:
        logger_access.info("⚠️  No executable files found in strategy directory")
        return False
    
    logger_access.info(f"🎯 Found executable files:")
    if python_files:
        logger_access.info(f"  📁 {len(python_files)} Python files:")
        for file in python_files:
            logger_access.info(f"    - {file.name}")
    if valid_notebooks:
        logger_access.info(f"  📓 {len(valid_notebooks)} Jupyter notebooks:")
        for file in valid_notebooks:
            logger_access.info(f"    - {file.name}")
    
    success_count = 0
    failed_files = []
    
    # Execute Python files first (they have priority)
    for script_path in python_files:
        logger_access.info(f"\n{'='*60}")
        logger_access.info(f"🔄 Executing Python file: {script_path.name}")
        logger_access.info(f"{'='*60}")
        
        if execute_strategy_file(script_path, config):
            success_count += 1
        else:
            failed_files.append(script_path.name)
    
    # Execute notebooks if no Python files or if Python files failed
    if not python_files or success_count == 0:
        for notebook_path in valid_notebooks:
            logger_access.info(f"\n{'='*60}")
            logger_access.info(f"🔄 Executing Jupyter notebook: {notebook_path.name}")
            logger_access.info(f"{'='*60}")
            
            if execute_notebook_file(notebook_path, config):
                success_count += 1
            else:
                failed_files.append(notebook_path.name)
    else:
        logger_access.info(f"\n📓 Skipping notebooks since Python files were executed successfully")
    
    logger_access.info(f"\n{'='*60}")
    logger_access.info(f"📊 Execution Summary:")
    logger_access.info(f"✅ Successfully executed: {success_count}/{total_files} files")
    if failed_files:
        logger_access.info(f"❌ Failed files: {', '.join(failed_files)}")
    logger_access.info(f"{'='*60}")
    
    return success_count > 0

def main():
    """Main function"""
    logger_access.info("🎯 Strategy Runner Starting...")
    logger_access.info("=" * 50)

    # Initialize params from config file or CLI arguments
    params = None
    try:
        args = sys.argv[1:]
        
        if args and args[0].endswith('.json'):
            config_path = args[0]
            if os.path.exists(config_path):
                loaded_config = load_config(config_path)
                if loaded_config:
                    params = {
                        "API_KEY": loaded_config.get("api_key", ""),
                        "SECRET_KEY": loaded_config.get("api_secret", ""),
                        "PASSPHRASE": loaded_config.get("passphrase", ""),
                        "EXCHANGE": loaded_config.get("exchange", ""),
                        "PAPER_MODE": loaded_config.get("paper_trading", False),
                        "SESSION_ID": loaded_config.get("session_key", ""),
                        "STRATEGY_NAME": loaded_config.get("strategy_name", ""),
                    }
                    logger_access.info(f"✅ Loaded config from JSON file: {config_path}")
            else:
                logger_access.error(f"❌ Config file not found: {config_path}")
                return 1
        elif len(args) >= 7:
            set_constants(args)
            params = get_constants()
        else:
            logger_access.error(f"❌ Invalid arguments: expected config file path or 7+ arguments")
            logger_access.error(f"   Received {len(args)} arguments: {args}")
            return 1
    except IndexError as e:
        logger_access.error(f"❌ Failed to parse CLI params (IndexError): {e}")
        logger_access.error(f"   Expected at least 7 command-line arguments")
        logger_access.error(f"📋 Traceback:\n{traceback.format_exc()}")
        return 1
    except Exception as e:
        logger_access.error(f"❌ Failed to parse CLI params: {e}")
        logger_access.error(f"📋 Traceback:\n{traceback.format_exc()}")
        return 1

    if not params:
        logger_access.error(f"❌ Failed to initialize parameters")
        return 1

    logger_access.info("
" + "=" * 50)
    logger_access.info("🌍 Hello World from Python Strategy Runner!")
    logger_access.info("=" * 50)

    # Log key parameters
    logger_access.info(f"📊 Session ID: {params.get('SESSION_ID', 'N/A')}")
    logger_access.info(f"🎯 Strategy Name: {params.get('STRATEGY_NAME', 'N/A')}")
    logger_access.info(f"🏦 Exchange: {params.get('EXCHANGE', 'N/A')}")
    logger_access.info(f"📝 Paper Mode: {params.get('PAPER_MODE', False)}")

    # Determine strategy directory
    strategy_name = params.get('STRATEGY_NAME', '') or ''

    # First, check if STRATEGY_DIR environment variable is set
    strategy_dir_env = os.environ.get('STRATEGY_DIR')
    if strategy_dir_env:
        strategy_dir = Path(strategy_dir_env)
        logger_access.info(f"🔍 Using strategy directory from environment: {strategy_dir}")
    elif strategy_name:
        strategy_dir = find_strategy_directory(strategy_name)
    else:
        strategy_dir = None

    logger_access.info(f"🔍 Strategy directory: {strategy_dir}")
    if strategy_dir and strategy_dir.exists():
        success = execute_all_strategies(strategy_dir, params)
        if success:
            logger_access.info("
✅ All strategy files executed successfully!")
            return 0
        else:
            logger_access.info("
❌ Strategy execution failed!")
            return 1
    else:
        logger_access.info(f"
⚠️  Strategy directory not found, running in demo mode")

    logger_access.info("
🎉 Demo execution completed successfully!")
    logger_access.info("=" * 50)
    return 0

if __name__ == "__main__":
    logger_access.info("=" * 50)
    exit(main())