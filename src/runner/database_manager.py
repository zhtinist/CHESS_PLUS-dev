import os
import pickle
from threading import Lock
from pathlib import Path
from dotenv import load_dotenv
from langchain_chroma import Chroma
from typing import Callable, Dict, List, Any

from database_utils.schema import DatabaseSchema
from database_utils.schema_generator import DatabaseSchemaGenerator
from database_utils.execution import execute_sql, compare_sqls, validate_sql_query, aggregate_sqls
from database_utils.db_info import get_db_all_tables, get_table_all_columns, get_db_schema
from database_utils.sql_parser import get_sql_tables, get_sql_columns_dict, get_sql_condition_literals
from database_utils.db_values.search import query_lsh
from database_utils.db_catalog.search import query_vector_db
from database_utils.db_catalog.preprocess import EMBEDDING_FUNCTION
from database_utils.db_catalog.csv_utils import load_tables_description

load_dotenv(override=True)
DB_ROOT_PATH = Path(os.getenv("DB_ROOT_PATH"))

class DatabaseManager:
    """
    A singleton class to manage database operations including schema generation, 
    querying LSH and vector databases, and managing column profiles.
    """
    _instance = None
    _lock = Lock()

    def __new__(cls, db_mode=None, db_id=None):
        if (db_mode is not None) and (db_id is not None):
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(DatabaseManager, cls).__new__(cls)
                    cls._instance._init(db_mode, db_id)
                elif cls._instance.db_id != db_id:
                    cls._instance._init(db_mode, db_id)
                return cls._instance
        else:
            if cls._instance is None:
                raise ValueError("DatabaseManager instance has not been initialized yet.")
            return cls._instance

    def _init(self, db_mode: str, db_id: str):
        """
        Initializes the DatabaseManager instance.

        Args:
            db_mode (str): The mode of the database (e.g., 'train', 'test').
            db_id (str): The database identifier.
        """
        self.db_mode = db_mode
        self.db_id = db_id
        self._set_paths()
        self.lsh = None
        self.minhashes = None
        self.vector_db = None

    def _set_paths(self):
        """Sets the paths for the database files and directories."""
        self.db_path = DB_ROOT_PATH / f"{self.db_mode}_databases" / self.db_id / f"{self.db_id}.sqlite"
        self.db_directory_path = DB_ROOT_PATH / f"{self.db_mode}_databases" / self.db_id

    def set_lsh(self) -> str:
        """Sets the LSH and minhashes attributes by loading from pickle files."""
        with self._lock:
            if self.lsh is None:
                try:
                    with (self.db_directory_path / "preprocessed" / f"{self.db_id}_lsh.pkl").open("rb") as file:
                        self.lsh = pickle.load(file)
                    with (self.db_directory_path / "preprocessed" / f"{self.db_id}_minhashes.pkl").open("rb") as file:
                        self.minhashes = pickle.load(file)
                    return "success"
                except Exception as e:
                    self.lsh = "error"
                    self.minhashes = "error"
                    print(f"Error loading LSH for {self.db_id}: {e}")
                    return "error"
            elif self.lsh == "error":
                return "error"
            else:
                return "success"

    def set_vector_db(self) -> str:
        """Sets the vector_db attribute by loading from the context vector database."""
        if self.vector_db is None:
            try:
                vector_db_path = self.db_directory_path / "context_vector_db"
                self.vector_db = Chroma(persist_directory=str(vector_db_path), embedding_function=EMBEDDING_FUNCTION)
                return "success"
            except Exception as e:
                self.vector_db = "error"
                print(f"Error loading Vector DB for {self.db_id}: {e}")
                return "error"
        elif self.vector_db == "error":
            return "error"
        else:
            return "success"

    def query_lsh(self, keyword: str, signature_size: int = 20, n_gram: int = 3, top_n: int = 10) -> Dict[str, List[str]]:
        """
        Queries the LSH for similar values to the given keyword.

        Args:
            keyword (str): The keyword to search for.
            signature_size (int, optional): The size of the MinHash signature. Defaults to 20.
            n_gram (int, optional): The n-gram size for the MinHash. Defaults to 3.
            top_n (int, optional): The number of top results to return. Defaults to 10.

        Returns:
            Dict[str, List[str]]: The dictionary of similar values.
        """
        lsh_status = self.set_lsh()
        if lsh_status == "success":
            return query_lsh(self.lsh, self.minhashes, keyword, signature_size, n_gram, top_n)
        else:
            raise Exception(f"Error loading LSH for {self.db_id}")

    def query_vector_db(self, keyword: str, top_k: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """
        Queries the vector database for similar values to the given keyword.

        Args:
            keyword (str): The keyword to search for.
            top_k (int): The number of top results to return.

        Returns:
            Dict[str, Dict[str, Dict[str, Any]]]: The dictionary of similar values.
        """
        vector_db_status = self.set_vector_db()
        if vector_db_status == "success":
            return query_vector_db(self.vector_db, keyword, top_k)
        else:
            raise Exception(f"Error loading Vector DB for {self.db_id}")

    def get_column_profiles(self, schema_with_examples: Dict[str, List[str]], 
                            use_value_description: bool, with_keys: bool, 
                            with_references: bool) -> Dict[str, Dict[str, str]]:
        """
        Generates column profiles for the schema.

        Args:
            schema_with_examples (Dict[str, List[str]]): Schema with example values.
            use_value_description (bool): Whether to use value descriptions.
            with_keys (bool): Whether to include keys.
            with_references (bool): Whether to include references.

        Returns:
            Dict[str, Dict[str, str]]: The dictionary of column profiles.
        """
        schema_with_descriptions = load_tables_description(self.db_directory_path, use_value_description)
        database_schema_generator = DatabaseSchemaGenerator(
            tentative_schema=DatabaseSchema.from_schema_dict(self.get_db_schema()),
            schema_with_examples=DatabaseSchema.from_schema_dict_with_examples(schema_with_examples),
            schema_with_descriptions=DatabaseSchema.from_schema_dict_with_descriptions(schema_with_descriptions),
            db_id=self.db_id,
            db_path=self.db_path,
            add_examples=True,
        )
        
        column_profiles = database_schema_generator.get_column_profiles(with_keys, with_references)
        return column_profiles

    def get_database_schema_string(self, tentative_schema: Dict[str, List[str]], 
                                   schema_with_examples: Dict[str, List[str]], 
                                   schema_with_descriptions: Dict[str, Dict[str, Dict[str, Any]]], 
                                   include_value_description: bool) -> str:
        """
        Generates a schema string for the database.

        Args:
            tentative_schema (Dict[str, List[str]]): The tentative schema.
            schema_with_examples (Dict[str, List[str]]): Schema with example values.
            schema_with_descriptions (Dict[str, Dict[str, Dict[str, Any]]]): Schema with descriptions.
            include_value_description (bool): Whether to include value descriptions.

        Returns:
            str: The generated schema string.
        """
        schema_generator = DatabaseSchemaGenerator(
            tentative_schema=DatabaseSchema.from_schema_dict(tentative_schema),
            schema_with_examples=DatabaseSchema.from_schema_dict_with_examples(schema_with_examples) if schema_with_examples else None,
            schema_with_descriptions=DatabaseSchema.from_schema_dict_with_descriptions(schema_with_descriptions) if schema_with_descriptions else None,
            db_id=self.db_id,
            db_path=self.db_path,
        )
        schema_string = schema_generator.generate_schema_string(include_value_description=include_value_description)
        return schema_string
    
    def add_connections_to_tentative_schema(self, tentative_schema: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """
        Adds connections to the tentative schema.

        Args:
            tentative_schema (Dict[str, List[str]]): The tentative schema.

        Returns:
            Dict[str, List[str]]: The updated schema with connections.
        """
        schema_generator = DatabaseSchemaGenerator(
            tentative_schema=DatabaseSchema.from_schema_dict(tentative_schema),
            db_id=self.db_id,
            db_path=self.db_path,
        )
        return schema_generator.get_schema_with_connections()

    @staticmethod
    def with_db_path(func: Callable):
        """
        Decorator to inject db_path as the first argument to the function.
        """
        def wrapper(self, *args, **kwargs):
            return func(self.db_path, *args, **kwargs)
        return wrapper

    @classmethod
    def add_methods_to_class(cls, funcs: List[Callable]):
        """
        Adds methods to the class with db_path automatically provided.

        Args:
            funcs (List[Callable]): List of functions to be added as methods.
        """
        for func in funcs:
            method = cls.with_db_path(func)
            setattr(cls, func.__name__, method)

# List of functions to be added to the class
functions_to_add = [
    execute_sql, 
    compare_sqls,
    validate_sql_query,
    aggregate_sqls,
    get_db_all_tables,
    get_table_all_columns,
    get_db_schema,
    get_sql_tables,
    get_sql_columns_dict,
    get_sql_condition_literals
]

# Adding methods to the class
DatabaseManager.add_methods_to_class(functions_to_add)
