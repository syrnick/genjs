#
#    Copyright 2016 Rethink Robotics
#
#    Copyright 2016 Chris Smith
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#    http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import sys
import os
import traceback
import re
from os.path import join as pjoin

#import roslib.msgs
#import roslib.srvs
#import roslib.packages
#import roslib.gentools
from genmsg import SrvSpec, MsgSpec, MsgContext
from genmsg.msg_loader import load_srv_from_file, load_msg_by_type
import genmsg.gentools
from copy import deepcopy

try:
    from cStringIO import StringIO #Python 2.x
except ImportError:
    from io import StringIO #Python 3.x

############################################################
# Built in types
############################################################

def is_fixnum(t):
    return t in ['int8', 'uint8', 'int16', 'uint16']

def is_integer(t):
    return is_fixnum(t) or t in ['byte', 'char', 'int32', 'uint32', 'int64', 'uint64'] #t2 byte, char can be fixnum

def is_signed_int(t):
    return t in ['int8', 'int16', 'int32', 'int64']

def is_unsigned_int(t):
    return t in ['uint8', 'uint16', 'uint32', 'uint64']

def is_bool(t):
    return t == 'bool'

def is_string(t):
    return t == 'string'

def is_float(t):
    return t in ['float32', 'float64']

def is_time(t):
    return t in ['time', 'duration']

def parse_msg_type(f):
    if f.base_type == 'Header':
        return ('std_msgs', 'Header')
    else:
        return f.base_type.split('/')

# t2 no need for is_array
def msg_type(f):
    (pkg, msg) = parse_msg_type(f)
    return '%s-msg:%s'%(pkg, msg)

def get_typed_array(t):
    if t in ['int8', 'byte', 'bool']:
        return 'Int8Array'
    elif t in ['uint8', 'char']:
        return 'UInt8Array'
    elif t == 'uint16':
        return 'UInt16Array'
    elif t == 'int16':
        return 'Int16Array'
    elif t == 'uint32':
        return 'UInt32Array'
    elif t == 'int32':
        return 'Int32Array'
    elif t == 'float32':
        return 'Float32Array'
    elif t == 'float64':
        return 'Float64Array'
    # else
    return None


def has_typed_array(t):
    return is_fixnum(t) or is_float(t) or t in ['byte', 'char', 'bool', 'uint8', 'uint16','int8', 'int16', 'uint32', 'int32']

NUM_BYTES = {'int8': 1, 'int16': 2, 'int32': 4, 'int64': 8,
             'uint8': 1, 'uint16': 2, 'uint32': 4, 'uint64': 8,
             'byte': 1, 'bool': 1, 'char': 1, 'float32': 4, 'float64': 4}

def get_default_value(field, current_message_package):
    if field.is_array:
        if not field.array_len:
            return '[]'
        else:
            field_copy = deepcopy(field)
            field_copy.is_array = False;
            field_default = get_default_value(field_copy, current_message_package)
            return 'new Array({}).fill({})'.format(field.array_len, field_default)
    elif field.is_builtin:
        if is_string(field.type):
            return '\'\''
        elif is_time(field.type):
            return '{secs: 0, nsecs: 0}'
        elif is_bool(field.type):
            return 'false'
        elif is_float(field.type):
            return '0.0'
        else:
            return '0';
    # else
    (package, msg_type) = field.base_type.split('/')
    if package == current_message_package:
        return 'new {}()'.format(msg_type)
    else:
        return 'new {}.msg.{}()'.format(package, msg_type)




############################################################
# Indented writer
############################################################

class IndentedWriter():

    def __init__(self, s):
        self.str = s
        self.indentation = 0
        self.block_indent = False

    def write(self, s, indent=True, newline=True):
        if not indent:
            newline = False
        if self.block_indent:
            self.block_indent = False
        else:
            if newline:
                self.str.write('\n')
            if indent:
                for i in range(self.indentation):
                    self.str.write(' ')
        self.str.write(s)

    def newline(self):
        self.str.write('\n')

    def inc_indent(self, inc=2):
        self.indentation += inc

    def dec_indent(self, dec=2):
        self.indentation -= dec

    def reset_indent(self):
        self.indentation = 0

    def block_next_indent(self):
        self.block_indent = True

class Indent():

    def __init__(self, w, inc=2, indent_first=True):
        self.writer = w
        self.inc = inc
        self.indent_first = indent_first

    def __enter__(self):
        self.writer.inc_indent(self.inc)
        if not self.indent_first:
            self.writer.block_next_indent()

    def __exit__(self, type, val, traceback):
        self.writer.dec_indent(self.inc)

def find_path_from_cmake_path(path):
    cmake_path = os.environ['CMAKE_PREFIX_PATH']
    paths = cmake_path.split(':')
    for search_path in paths:
        test_path = pjoin(search_path, path)
        if os.path.exists(test_path):
            return test_path
    return None

def find_path_for_package(package):
    return find_path_from_cmake_path(pjoin('share/node_js/ros', package))

def find_requires(spec):
    found_packages = {}
    local_deps = []
    for field in spec.parsed_fields():
        if not field.is_builtin:
            (field_type_package, msg_type) = field.base_type.split('/')
            if field_type_package in found_packages:
                continue
            # else
            if field_type_package == spec.package:
                if msg_type not in local_deps:
                    local_deps.append(msg_type)
            else:
                path = find_path_for_package(field_type_package)
                if path is None:
                    print 'Couldn\'t find path for type ', field.base_type
                else:
                    found_packages[field_type_package] = path

    return found_packages, local_deps

def write_begin(s, spec, is_service=False):
    "Writes the beginning of the file: a comment saying it's auto-generated and the in-package form"

    s.write('// Auto-generated. Do not edit!\n\n', newline=False)
    suffix = 'srv' if is_service else 'msg'
    s.write('// (in-package %s.%s)\n\n'%(spec.package, suffix), newline=False)

def write_requires(s, spec, previous_packages=None, prev_deps=None, isSrv=False):
    "Writes out the require fields"
    if previous_packages is None:
        s.write('"use strict";')
        s.newline()
        s.write('let _serializer = require(\'base_serialize\');')
        s.write('let _deserializer = require(\'base_deserialize\');')
        s.write('let _finder = require(\'find\');')
        previous_packages = {}
    if prev_deps is None:
        prev_deps = []
    # find other message packages and other messages in this packages
    # that this message depends on
    found_packages, local_deps = find_requires(spec)
    # filter out previously found local deps
    local_deps = [dep for dep in local_deps if dep not in prev_deps]

    # require mesages from this package
    # messages from this package need to be requried separately
    # so that we don't create a circular requires dependency
    for dep in local_deps:
        if isSrv:
            s.write('let {} = require(\'../msg/{}.msg\');'.format(dep, dep))
        else:
            s.write('let {} = require(\'./{}.msg\');'.format(dep, dep))

    # filter out previously found packages
    found_packages = {key: val for (key, val) in found_packages.items() if key not in previous_packages}
    for (package, path) in found_packages.items():
        # TODO: finder is only relevant to node - we should support an option to
        #   create a flat message package directory. The downside is that it requires
        #   copying files between workspaces.
        s.write('let {0} = _finder(\'{0}\');'.format(package))
    s.newline()
    s.write('//-----------------------------------------------------------')
    s.newline()
    return found_packages, local_deps

def write_msg_constructor_field(s, spec, field):
    s.write('this.{} = {};'.format(field.name, get_default_value(field, spec.package)))

def write_class(s, spec):
    s.write('class {} {{'.format(spec.actual_name))
    with Indent(s):
        s.write('constructor() {')
        with Indent(s):
            for field in spec.parsed_fields():
                write_msg_constructor_field(s, spec, field)
        s.write('}')
    s.newline()

def write_end(s, spec):
    s.write('};')
    s.newline();
    write_constants(s, spec)
    s.write('module.exports = {};'.format(spec.actual_name))

def write_serialize_base(s, rest):
    s.write('bufferInfo = {};'.format(rest))

def write_serialize_length(s, name):
    #t2
    s.write('// Serialize the length for message field [{}]'.format(name))
    write_serialize_base(s, '_serializer.uint32(obj.{}.length, bufferInfo)'.format(name))

# adds function to serialize builtin types (string, uint8, ...)
def write_serialize_builtin(s, f):
    if (f.is_array):
        if f.base_type == 'uint8':
            # FIXME: do this for more than uint8
            s.write('bufferInfo.buffer.push(obj.{});'.format(f.name))
            s.write('bufferInfo.length += obj.{}.length;'.format(f.name))
        else:
            s.write('obj.{}.forEach((val) => {{'.format(f.name))
            with Indent(s):
                write_serialize_base(s, '_serializer.{}(val, bufferInfo)'.format(f.base_type))
            s.write('});')
    else:
        write_serialize_base(s, '_serializer.{}(obj.{}, bufferInfo)'.format(f.type, f.name))

# adds function to serlialize complex type (geometry_msgs/Pose)
def write_serialize_complex(s, f, thisPackage):
    (package, msg_type) = f.base_type.split('/')
    samePackage =  package == thisPackage
    if (f.is_array):
        s.write('obj.{}.forEach((val) => {{'.format(f.name))
        with Indent(s):
            if samePackage:
                write_serialize_base(s, '{}.serialize(val, bufferInfo)'.format(msg_type))
            else:
                write_serialize_base(s, '{}.msg.{}.serialize(val, bufferInfo)'.format(package, msg_type))
        s.write('});')
    else:
        if samePackage:
            write_serialize_base(s, '{}.serialize(obj.{}, bufferInfo)'.format(msg_type, f.name))
        else:
            write_serialize_base(s, '{}.msg.{}.serialize(obj.{}, bufferInfo)'.format(package, msg_type, f.name))

# writes serialization for a single field in the message
def write_serialize_field(s, f, package):
    if f.is_array:
        if not f.array_len:
            write_serialize_length(s, f.name)

    s.write('// Serialize message field [{}]'.format(f.name))
    if f.is_builtin:
        write_serialize_builtin(s, f)
    else:
        write_serialize_complex(s, f, package)

def write_serialize(s, spec):
    """
    Write the serialize method
    """
    with Indent(s):
        s.write('static serialize(obj, bufferInfo) {')
        with Indent(s):
            s.write('// Serializes a message object of type {}'.format(spec.short_name))
            for f in spec.parsed_fields():
                write_serialize_field(s, f, spec.package)
            s.write('return bufferInfo;')
        s.write('}')
        s.newline()

# t2 can get rid of is_array
def write_deserialize_length(s, name):
    s.write('// Deserialize array length for message field [{}]'.format(name))
    s.write('tmp = _deserializer.uint32(buffer);')
    s.write('len = tmp.data;')
    s.write('buffer = tmp.buffer;')

def write_deserialize_complex(s, f, thisPackage):
    (package, msg_type) = f.base_type.split('/')
    samePackage = package == thisPackage
    if f.is_array:
        s.write('data.{} = new Array(len);'.format(f.name))
        s.write('for (let i = 0; i < len; ++i) {')
        with Indent(s):
            if samePackage:
                s.write('tmp = {}.deserialize(buffer);'.format(msg_type))
            else:
                s.write('tmp = {}.msg.{}.deserialize(buffer);'.format(package, msg_type))
            s.write('data.{}[i] = tmp.data;'.format(f.name))
            s.write('buffer = tmp.buffer;')
        s.write('}')
    else:
        if samePackage:
            s.write('tmp = {}.deserialize(buffer);'.format(msg_type))
        else:
            s.write('tmp = {}.msg.{}.deserialize(buffer);'.format(package, msg_type))
        s.write('data.{} = tmp.data;'.format(f.name))
        s.write('buffer = tmp.buffer;')

def write_deserialize_builtin(s, f):
    if f.is_array:
        if f.base_type == 'uint8':
            # FIXME: do this for more than just uint8
            s.write('data.{} = buffer.slice(0, len);'.format(f.name))
            s.write('buffer =  buffer.slice(len);')
        else:
            s.write('data.{} = new Array(len);'.format(f.name))
            s.write('for (let i = 0; i < len; ++i) {')
            with Indent(s):
                s.write('tmp = _deserializer.{}(buffer);'.format(f.base_type))
                s.write('data.{}[i] = tmp.data;'.format(f.name))
                s.write('buffer = tmp.buffer;')
            s.write('}')
    else:
        s.write('tmp = _deserializer.{}(buffer);'.format(f.base_type))
        s.write('data.{} = tmp.data;'.format(f.name))
        s.write('buffer = tmp.buffer;')


def write_deserialize_field(s, f, package):
    if f.is_array:
        if not f.array_len:
            write_deserialize_length(s, f.name)
        else:
            s.write('len = {};'.format(f.array_len))

    s.write('// Deserialize message field [{}]'.format(f.name))
    if f.is_builtin:
        write_deserialize_builtin(s, f)
    else:
        write_deserialize_complex(s, f, package)


def write_deserialize(s, spec):
    """
    Write the deserialize method
    """
    with Indent(s):
        s.write('static deserialize(buffer) {')
        with Indent(s):
            s.write('//deserializes a message object of type {}'.format(spec.short_name))
            s.write('let tmp;')
            s.write('let len;')
            s.write('let data = {};')
            for f in spec.parsed_fields():
                write_deserialize_field(s, f, spec.package)

            s.write('return {')
            with Indent(s):
                s.write('data: data,')
                s.write('buffer: buffer')
            s.write('}')
        s.write('}')
        s.newline()

def write_package_index(s, package_dir):
    s.write('"use strict";')
    s.newline()
    s.write('module.exports = {')
    msgExists = os.path.exists(pjoin(package_dir, 'msg/_index.js'))
    srvExists = os.path.exists(pjoin(package_dir, 'srv/_index.js'))
    with Indent(s):
        if (msgExists):
            s.write('msg: require(\'./msg/_index.js\'),')
        if (srvExists):
            s.write('srv: require(\'./srv/_index.js\')')
    s.write('};')
    s.newline()

def write_msg_index(s, msgs, pkg, context):
    "Writes an index for the messages"
    s.write('"use strict";')
    s.newline()

    for msg in msgs:
        s.write('let {} = require(\'./{}.js\');'.format(msg, msg))
    s.newline()
    s.write('module.exports = {')
    with Indent(s):
        for msg in msgs:
            s.write('{}: {},'.format(msg, msg))
    s.write('};')
    s.newline()

def write_srv_index(s, srvs, pkg):
    "Writes an index for the messages"
    s.write('"use strict";')
    s.newline()
    for srv in srvs:
        s.write('let {} = require(\'./{}.js\')'.format(srv, srv))
    s.newline()
    s.write('module.exports = {')
    with Indent(s):
        for srv in srvs:
            s.write('{}: {},'.format(srv, srv))
    s.write('};')
    s.newline()

def write_ros_datatype(s, spec):
    with Indent(s):
        s.write('static datatype() {')
        with Indent(s):
            s.write('// Returns string type for a %s object'%spec.component_type)
            s.write('return \'{}\';'.format(spec.full_name))
        s.write('}')
        s.newline()

def write_md5sum(s, msg_context, spec, parent=None):
    md5sum = 'TODO' #genmsg.compute_md5(msg_context, parent or spec)
    with Indent(s):
        s.write('static md5sum() {')
        with Indent(s):
            # t2 this should print 'service' instead of 'message' if it's a service request or response
            s.write('//Returns md5sum for a message object')
            s.write('return \'{}\';'.format(md5sum))
        s.write('}')
        s.newline()

def write_message_definition(s, msg_context, spec):
    with Indent(s):
        s.write('static messageDefinition() {')
        with Indent(s):
            s.write('// Returns full string definition for message')
            definition = '' #genmsg.compute_full_text(msg_context, spec)
            lines = definition.split('\n')
            s.write('return `')
            for line in lines:
                s.write('{}'.format(line))
            s.write('`;')
        s.write('}')
        s.newline()

def write_constants(s, spec):
    if spec.constants:
        s.write('// Constants for message')
        s.write('{}.Constants = {{'.format(spec.short_name))
        with Indent(s):
            for c in spec.constants:
                if is_string(c.type):
                    s.write('{}: \'{}\','.format(c.name.upper(), c.val))
                else:
                    s.write('{}: {},'.format(c.name.upper(), c.val))
        s.write('}')
        s.newline()

def write_srv_component(s, spec, context, parent):
    spec.component_type='service'
    write_class(s, spec)
    write_serialize(s, spec)
    write_deserialize(s, spec)
    write_ros_datatype(s, spec)
    write_md5sum(s, context, spec)
    write_message_definition(s, context, spec)
    s.write('};')
    s.newline()
    write_constants(s, spec)

def write_srv_end(s, name):
    s.write('module.exports = {')
    with Indent(s):
        s.write('Request: {}Request,'.format(name))
        s.write('Response: {}Response'.format(name))
    s.write('};')
    s.newline()

def generate_msg(pkg, files, out_dir, search_path):
    """
    Generate javascript code for all messages in a package
    """
    msg_context = MsgContext.create_default()
    for f in files:
        f = os.path.abspath(f)
        infile = os.path.basename(f)
        full_type = genmsg.gentools.compute_full_type_name(pkg, infile)
        spec = genmsg.msg_loader.load_msg_from_file(msg_context, f, full_type)
        generate_msg_from_spec(msg_context, spec, search_path, out_dir, pkg)

def generate_srv(pkg, files, out_dir, search_path):
    """
    Generate javascript code for all services in a package
    """
    msg_context = MsgContext.create_default()
    for f in files:
        f = os.path.abspath(f)
        infile = os.path.basename(f)
        full_type = genmsg.gentools.compute_full_type_name(pkg, infile)
        spec = genmsg.msg_loader.load_srv_from_file(msg_context, f, full_type)
        generate_srv_from_spec(msg_context, spec, search_path, out_dir, pkg, f)

def msg_list(pkg, search_path, ext):  
    dir_list = search_path[pkg]
    files = []
    for d in dir_list:
        files.extend([f for f in os.listdir(d) if f.endswith(ext)])
    return [f[:-len(ext)] for f in files]

def generate_msg_from_spec(msg_context, spec, search_path, output_dir, package, msgs=None):
    """
    Generate a message

    @param msg_path: The path to the .msg file
    @type msg_path: str
    """
    print >> sys.stderr, ['load_depends', msg_context, spec, search_path]
    #    genmsg.msg_loader.load_depends(msg_context, spec, search_path)
    spec.actual_name=spec.short_name
    spec.component_type='message'
    msgs = msg_list(package, search_path, '.msg')
    for m in msgs:
        genmsg.load_msg_by_type(msg_context, '%s/%s'%(package, m), search_path)

    ########################################
    # 1. Write the .js file
    ########################################

    io = StringIO()
    s =  IndentedWriter(io)
    write_begin(s, spec)
    write_requires(s, spec)
    write_class(s, spec)
    write_serialize(s, spec)
    write_deserialize(s, spec)
    write_ros_datatype(s, spec)
    write_md5sum(s, msg_context, spec)
    write_message_definition(s, msg_context, spec)
    write_end(s, spec)

    if (not os.path.exists(output_dir)):
        # if we're being run concurrently, the above test can report false but os.makedirs can still fail if
        # another copy just created the directory
        try:
            os.makedirs(output_dir)
        except OSError as e:
            pass

    with open('%s/%s.js'%(output_dir, spec.short_name), 'w') as f:
        f.write(io.getvalue() + "\n")
    io.close()

    ########################################
    # 3. Write the msg/_index.js file
    # This is being rewritten once per msg
    # file, which is inefficient
    ########################################
    io = StringIO()
    s = IndentedWriter(io)
    write_msg_index(s, msgs, package, msg_context)
    with open('{}/_index.js'.format(output_dir), 'w') as f:
        f.write(io.getvalue())
    io.close()

    ########################################
    # 3. Write the package _index.js file
    # This is being rewritten once per msg
    # file, which is inefficient
    ########################################
    io = StringIO()
    s = IndentedWriter(io)
    package_dir = os.path.dirname(output_dir)
    write_package_index(s, package_dir)
    with open('{}/_index.js'.format(package_dir), 'w') as f:
        f.write(io.getvalue())
    io.close()

# t0 most of this could probably be refactored into being shared with messages
def generate_srv_from_spec(msg_context, spec, search_path, output_dir, package, path):
    "Generate code from .srv file"
    genmsg.msg_loader.load_depends(msg_context, spec, search_path)
    ext = '.srv'
    srv_path = os.path.dirname(path)
    srvs = msg_list(package, {package: [srv_path]}, ext)
    for srv in srvs:
        load_srv_from_file(msg_context, '%s/%s%s'%(srv_path, srv, ext), '%s/%s'%(package, srv))

    ########################################
    # 1. Write the .js file
    ########################################

    io = StringIO()
    s = IndentedWriter(io)
    write_begin(s, spec, True)
    found_packages,local_deps = write_requires(s, spec.request, None, None, True)
    write_requires(s, spec.response, found_packages, local_deps, True)
    spec.request.actual_name='%sRequest'%spec.short_name
    spec.response.actual_name='%sResponse'%spec.short_name
    write_srv_component(s, spec.request, msg_context, spec)
    write_srv_component(s, spec.response, msg_context, spec)
    write_srv_end(s, spec.short_name)

    with open('%s/%s.js'%(output_dir, spec.short_name), 'w') as f:
        f.write(io.getvalue())
    io.close()

    ########################################
    # 3. Write the msg/_index.js file
    # This is being rewritten once per msg
    # file, which is inefficient
    ########################################
    io = StringIO()
    s = IndentedWriter(io)
    write_srv_index(s, srvs, package)
    with open('{}/_index.js'.format(output_dir), 'w') as f:
        f.write(io.getvalue())
    io.close()

    ########################################
    # 3. Write the package _index.js file
    # This is being rewritten once per msg
    # file, which is inefficient
    ########################################
    io = StringIO()
    s = IndentedWriter(io)
    package_dir = os.path.dirname(output_dir)
    write_package_index(s, package_dir)
    with open('{}/_index.js'.format(package_dir), 'w') as f:
        f.write(io.getvalue())
    io.close()
