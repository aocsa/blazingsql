
from pyhive import hive
from TCLIService.ttypes import TOperationState
import numpy as np
import cudf
from itertools import repeat
import pandas as pd

def convertHiveTypeToCudfType(hiveType):
    if(hiveType == 'int' or hiveType == 'integer'):
        return np.int32
    elif(hiveType == 'string' or hiveType.startswith('varchar') or hiveType == "char" or hiveType == "binary"):
        return np.str
    elif(hiveType == 'tinyint'):
        return np.int8
    elif(hiveType == 'smallint'):
        return np.int16
    elif(hiveType == 'bigint'):
        return np.int64
    elif(hiveType == 'float'):
        return np.float32
    elif(hiveType == 'double' or hiveType == 'double precision'):
        return np.float64
    elif(hiveType == 'decimal' or hiveType == 'numeric'):
        return None
    elif(hiveType == 'timestamp'):
        return np.datetime64
    elif(hiveType == 'date'):
        return np.datetime64
    elif(hiveType == 'boolean'):
        return np.bool
    elif(hiveType == 'date'):
        return np.datetime64


def getPartitions(tableName, schema, cursor):
    query = "show partitions " + tableName
    result = runHiveQuery(cursor, query)
    partitions = {}
    for partition in result[0]:
        columnPartitions = []
        for columnPartition in partition:
            for columnData in columnPartition.split("/"):
                columnName = columnData.split("=")[0]
                for column in schema['columns']:
                    if column[0] == columnName:
                        #print(columnData.split("=")[1])
                        if(column[1] == np.str):
                            columnValue = columnData.split("=")[1]
                        elif(column[1] == np.datetime64):
                            columnValue = np.datetime64(columnData.split("=")[1])
                        else:
                            columnValue = np.fromstring(
                            columnData.split("=")[1], column[1], sep=' ')[0]
                        columnPartitions.append((columnName, columnValue))
        partitions[partition[0]] = columnPartitions
    return partitions


dtypes = {
    np.float64: "double",
    np.float32: "float",
    np.int64: "int64",
    np.longlong: "int64",
    np.int32: "int32",
    np.int16: "int16",
    np.int8: "int8",
    np.bool_: "bool",
    np.datetime64: "date64",
    np.object_: "str",
    np.str_: "str",
}


def gdf_dtype_from_dtype(dtype):
    if pd.api.types.is_datetime64_dtype(dtype):
        time_unit, _ = np.datetime_data(dtype)
        return "date64"
    # everything else is a 1-1 mapping
    dtype = np.dtype(dtype)
    if dtype.type in dtypes:
        return dtypes[dtype.type]
    raise TypeError('cannot convert numpy dtype `%s` to gdf_dtype' % (dtype))


def get_hive_table(cursor, tableName):
    query = 'describe formatted ' + tableName
    result, description = runHiveQuery(cursor, query)
    schema = {}
    schema['columns'] = []
    i = 0
    # print(result)
    parsingColumns = False
    parsingPartitionColumns = False
    startParsingPartitionRows = 0
    schema['delimiter'] = chr(1)
    for triple in result:
        # print(triple)
        if triple[0] is not None:
            if(i == 2):
                parsingColumns = True
            if(parsingColumns):
                # print(triple)
                if triple[0] == '':
                    parsingColumns = False
                else:
                    schema['columns'].append(
                        (triple[0], convertHiveTypeToCudfType(triple[1]), False))
            elif isinstance(triple[0], str) and triple[0].startswith('Location:'):
                if triple[1].startswith("file:"):
                    schema['location'] = triple[1].replace("file:", "")
                else:
                    schema['location'] = triple[1]
            elif isinstance(triple[0], str) and triple[0].startswith('InputFormat:'):
                if "TextInputFormat" in triple[1]:
                  #                  schema['fileType'] = self.CSV_FILE_TYPE
                    schema['fileType'] = 'csv'
                if "ParquetInputFormat" in triple[1]:
                  #                  schema['fileType'] = self.PARQUET_FILE_TYPE
                    schema['fileType'] = 'parquet'
                if "OrcInputFormat" in triple[1]:
                  #                  schema['fileType'] = self.ORC_FILE_TYPE
                    schema['fileType'] = 'orc'
                if "JsonInputFormat" in triple[1]:
                  #                  schema['fileType'] = self.JSON_FILE_TYPE
                    schema['fileType'] = 'json'
            elif isinstance(triple[1], str) and triple[1].startswith("field.delim"):
                schema['delimiter'] = triple[2][0]
            elif triple[0] == "# Partition Information":
                parsingPartitionColumns = True
                startParsingPartitionRows = i + 2
            elif parsingPartitionColumns and i > startParsingPartitionRows:
                if triple[0] == "# Detailed Table Information":
                    parsingPartitionColumns = False
                elif triple[0] != "":
                    schema['columns'].append(
                        (triple[0], convertHiveTypeToCudfType(triple[1]), True))
        i = i + 1
    hasPartitions = False
    for column in schema['columns']:
        if column[2]:
            hasPartitions = True
    file_list = []
    if hasPartitions:
        schema['partitions'] = getPartitions(tableName, schema, cursor)
    else:
        schema['partitions'] = {}
        file_list.append(schema['location'] + "/*")

    uri_values = []
    extra_kwargs = {}
    extra_kwargs['delimiter'] = schema['delimiter']
    if schema['fileType'] == 'csv':
        extra_kwargs['names'] = [col_name for col_name, dtype, is_virtual_col  in schema['columns'] if not is_virtual_col ]
        extra_kwargs['dtype'] = [gdf_dtype_from_dtype(dtype) for col_name, dtype, is_virtual_col in schema['columns'] if not is_virtual_col]
    extra_kwargs['file_format'] = schema['fileType']
    extra_columns = []
    in_file = []
    for column in schema['columns']:
        in_file.append(column[2] == False)
        if(column[2]):
            extra_columns.append((column[0], column[1]))
    for partitionName in schema['partitions']:
        partition = schema['partitions'][partitionName]
        file_list.append(schema['location'] + "/" + partitionName + "/*")
        uri_values.append(partition)
    return file_list, uri_values, schema['fileType'], extra_kwargs, extra_columns, in_file, schema['partitions']


def runHiveDDL(cursor, query):
    cursor.execute(query, async_=True)
    status = cursor.poll().operationState
    while status in (
            TOperationState.INITIALIZED_STATE,
            TOperationState.RUNNING_STATE):
        status = cursor.poll().operationState


def runHiveQuery(cursor, query):
    cursor.execute(query, async_=True)
    status = cursor.poll().operationState
    while status in (
            TOperationState.INITIALIZED_STATE,
            TOperationState.RUNNING_STATE):
        status = cursor.poll().operationState
    return cursor.fetchall(), cursor.description


def convertHiveToCudf(cursor, query):
    df = cudf.DataFrame()
    result, description = runHiveQuery(cursor, query)
    arrays = [[] for i in repeat(None, len(result[0]))]
    for row in result:
        i = 0
        for val in row:
            arrays[i].append(val)
            i = i + 1
    i = 0
    while i < len(result[0]):
        column = cudf.Series(arrays[i])
        df[description[i][0].split('.')[1]] = column
        i = i + 1
    return df
