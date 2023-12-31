import warnings
import numpy as np
from cgp.cgp import Individual, FunctionGen, OutputGen
from keras.layers import MaxPooling2D, BatchNormalization, Conv2D, Activation
from keras.applications.mobilenet import DepthwiseConv2D
from keras.models import Model, model_from_json
import keras.backend as K
import tensorflow as tf
from fcn_utils.BilinearUpSampling import BilinearUpSampling2D
from layers.pad import PadZeros
from layers.shuffle import ChannelShuffle, SliceLayer
from threading import Lock

class Evaluator:
    """
    Class responsible for converting an Individual into a Keras Model and the evaluation
    """

    def __init__(self, function_mapping, trainer, add_batch_norm=True, input_shape=(64, 64, 3), can_growth=False):
        """
        Parameters
        ----------
        function_mapping: dict
            a dict containg functions and the corresponding inputs
        trainer: ClassifyTrainer
            a trainer which will be called after the conversion into an keras model is done
        add_batch_norm: bool(True)
            if true every conv layer follows a batch norm and a relu activation layer
        input_shape: tuple(64,64,3)
            input shape
        can_growth: bool(False)
             if true tensorflow allocate only as much GPU memory based on runtime allocations
        """
        self.function_mapping = function_mapping
        self.input_shape = input_shape
        self.trainer = trainer
        self.models = {}
        self.add_batch_norm = add_batch_norm
        self.mutex = Lock()
        self.can_growth = can_growth

    def __call__(self, child, child_number, epoch):
        """
        this method is called to evaluate a child and measure the score

        it contains thee logi wholc to translate a cgp individual into a keras model
        and the evaluation (train a model and create a score for a child) as well
        Parameters
        ----------
        child: Individual
            Individual to evaluate
        child_number: int
            child number, could be useful for multi gpu usage to assign each child a different gpu

        Returns
        -------
            score value for a given child
        """

        config = None
        if self.can_growth:
            config = tf.ConfigProto(gpu_options=tf.GPUOptions(allow_growth=True))

        with tf.Session(graph=tf.Graph(), config=config) as sess:
            K.set_session(sess)

            try:
                model = self.individual_to_keras_model(child, child_number)
                if model is None:
                    child.mutate()
                    return self(child, child_number, epoch)

                score, total_params, accuracy = self.trainer(model, epoch)

                if score is None:
                    child.mutate()
                    return self(child, child_number, epoch)

                with self.mutex:
                    self.models[child_number] = {'model': model.to_json(), 'weights': model.get_weights()}

                return score, total_params, model, accuracy
            except Exception as ex:
                print("exception", ex)
                #return self(child, child_number, epoch)
                return self.trainer.worst, self.trainer.worst,self.trainer.worst, self.trainer.worst

    def get_function_input_list(self):
        """
        generate a list containing the fpipunction names and a list containing the number of
        inputs for each function
        Returns
        -------

        """
        functions = list(self.function_mapping.keys())
        inputs = [value['inputs'] for key, value in self.function_mapping.items()]
        return functions, inputs

    def name_to_layer(self, name, idx):
        """
        creates an instance of a keras layer based on the constant function mapping

        Parameters
        ----------
        name: str
            layer name, must be the key in function_mapping
        idx:
            some index or id what ever to make a layer name unique

        Returns
        -------
        keras.layer
            a valid keras layer
        """
        if name not in self.function_mapping:
            raise ValueError('name %s not exists in function mapping' % name)

        layer = self.function_mapping[name]
        cls, args = layer['cls'], layer['args']
        args['name'] = name + '_id_%d' % idx
        return cls(**args)

    def individual_to_keras_model(self, individual, child_number=0):
        """
        creates a keras model based on an individual

        Parameters
        ----------
        individual: Individual
            instance of an individual where an keras model should be created
        child_number: int
            a valid index or id

        Returns
        -------
            an individual if a model could be translated otherwise None
        """
        try:
            if not isinstance(individual, Individual):
                raise TypeError("Individual must be the type Individual")

            from keras.layers import Input, MaxPool2D
            from keras.layers.merge import _Merge
            import tensorflow as tf

            active_nodes = np.where(individual.active)[0]

            nodes = {}
            for i in range(individual.config.num_input):
                node = Input(shape=self.input_shape, name='input-%d' % i)
                nodes[i] = node

            outputs = []
            for idx in active_nodes:
                n = individual.genes[idx]

                if isinstance(n, FunctionGen):
                    nodes[idx + individual.config.num_input] = individual.config.functions[n.fnc_idx]
                elif isinstance(n, OutputGen):
                    outputs.append(idx)

            for idx in active_nodes:
                if idx >= individual.config.num_nodes:
                    continue

                node = self.name_to_layer(nodes[idx + individual.config.num_input], idx)

                if isinstance(node, _Merge) and individual.genes[idx].num_inputs == 2:
                    x = []
                    shapes = []
                    for con in range(individual.genes[idx].num_inputs):
                        instance = nodes[individual.genes[idx].inputs[con]]
                        x.append(instance)
                        shapes.append(instance.shape.as_list())

                    _, a_width, a_height, a_channels = shapes[0]
                    _, b_width, b_height, b_channels = shapes[1]

                    if a_width > b_width:
                        x[0] = MaxPooling2D(pool_size=(a_height // b_height, a_width // b_width))(x[0])
                    if a_width < b_width:
                        x[1] = MaxPooling2D(pool_size=(b_height // a_height, b_width // a_width))(x[1])

                    if a_channels > b_channels:
                        diff = a_channels - b_channels
                        x[1] = PadZeros(diff, name='pad_%d' % idx)(x[1])
                        #x[1] = Conv2D(a_channels, kernel_size=1, padding='same', activation='relu',
                        #              kernel_initializer='he_uniform', name='pad_%d' % idx)(x[1])
                    elif a_channels < b_channels:
                        diff = b_channels - a_channels
                        x[0] = PadZeros(diff, name='pad_%d' % idx)(x[0])
                        #x[0] = Conv2D(b_channels, kernel_size=1, padding='same', activation='relu',
                        #              kernel_initializer='he_uniform', name='pad_%d' % idx)(x[0])

                elif individual.genes[idx].num_inputs == 1:
                    x = nodes[individual.genes[idx].inputs[0]]


                if self.add_batch_norm and isinstance(node, Conv2D):
                    x = node(x)
                    x = BatchNormalization(axis=-1, name='%s_bn' % node.name)(x)
                    x = Activation('relu',  name='%s_act' % node.name)(x)
                else:
                    x = node(x)

                nodes[idx + individual.config.num_input] = x

            keys = list(nodes.keys())
            inputs = [nodes[i] for i in keys[:individual.config.num_input]]
            outputs = []
            for out in [nodes[i] for i in sorted(keys)[-individual.config.num_output:]]:
                x = self.trainer.append_output_layer(out)
                outputs.append(x)

            model = Model(inputs=inputs, outputs=outputs, name='child-%d' % child_number)

            return model
        except Exception as ex:
            warnings.warn("can't build model:\n%s" % ex)
            return None

    def improved(self, child_number, score):
        """
        function is called if the cgp evolution strategy finds a better individual

        Parameters
        ----------
        child_number: int
            child index
        score: float
            score of the better individual

        Returns
        -------

        """
        with tf.Session(graph=tf.Graph()) as sess:
            K.set_session(sess)
            self.mutex.acquire()
            model, weights = self.models[child_number]['model'], self.models[child_number]['weights']
            self.mutex.release()
            model = model_from_json(model, custom_objects={
                'BilinearUpSampling2D': BilinearUpSampling2D,
                'PadZeros': PadZeros,
                'SliceLayer': SliceLayer,
                'DepthwiseConv2D': DepthwiseConv2D,
                'ChannelShuffle': ChannelShuffle})

            self.trainer.model_improved(model, score)


